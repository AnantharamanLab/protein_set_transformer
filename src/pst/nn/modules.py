from __future__ import annotations

import sys
from functools import cached_property
from pathlib import Path
from typing import (
    Any,
    Callable,
    Generic,
    Literal,
    Optional,
    Type,
    TypeVar,
    cast,
    get_type_hints,
)

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

import lightning as L
import torch
from lightning_cv import CrossValModuleMixin
from transformers import get_linear_schedule_with_warmup

from pst.data.modules import GenomeDataset
from pst.nn.config import BaseModelConfig, ModelConfig
from pst.nn.layers import PositionalEmbedding
from pst.nn.models import SetTransformer, SetTransformerDecoder, SetTransformerEncoder
from pst.nn.utils.distance import (
    pairwise_euclidean_distance,
    stacked_batch_chamfer_distance,
)
from pst.nn.utils.loss import AugmentedWeightedTripletLoss, WeightedTripletLoss
from pst.nn.utils.sampling import (
    negative_sampling,
    point_swap_sampling,
    positive_sampling,
)
from pst.typing import EdgeAttnOutput, GenomeGraphBatch, GraphAttnOutput, OptTensor

_STAGE_TYPE = Literal["train", "val", "test"]
_FIXED_POINTSWAP_RATE = "FIXED_POINTSWAP_RATE"
_ModelT = TypeVar("_ModelT", SetTransformer, SetTransformerEncoder)
_BaseConfigT = TypeVar("_BaseConfigT", bound=BaseModelConfig)


class PositionalStrandEmbeddingModule(L.LightningModule):
    def __init__(self, in_dim: int, embed_scale: int, max_size: int):
        super().__init__()

        self.max_size = max_size
        embedding_dim = in_dim // embed_scale

        self.positional_embedding = PositionalEmbedding(
            dim=embedding_dim, max_size=max_size
        )

        # embed +/- gene strand
        self.strand_embedding = torch.nn.Embedding(
            num_embeddings=2, embedding_dim=embedding_dim
        )

        self.extra_embedding_dim = 2 * embedding_dim

    def internal_embeddings(
        self, batch: GenomeGraphBatch
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Compute the strand and positional embeddings for the proteins in the batch.

        Then return the embeddings for the strand, the positional embeddings, and the concatenated
        embeddings of the proteins with the positional and strand embeddings.

        Args:
            batch (GenomeGraphBatch): The batch object containing the protein embeddings,
                edge index, the index pointer, the number of proteins in each genome, etc, that
                are used for the forward pass of the `SetTransformer` or `SetTransformerEncoder`.
                This object models the data patterns of PyTorch Geometric graphs.

        Returns:
            tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
                (concatenated embeddings, positional embeddings, strand embeddings)
        """
        strand_embed = self.strand_embedding(batch.strand)
        positional_embed = self.positional_embedding(batch.pos.squeeze())

        x_cat = self.concatenate_embeddings(
            x=batch.x, positional_embed=positional_embed, strand_embed=strand_embed
        )

        return x_cat, positional_embed, strand_embed

    def concatenate_embeddings(
        self,
        x: torch.Tensor,
        positional_embed: torch.Tensor,
        strand_embed: torch.Tensor,
    ) -> torch.Tensor:
        """Concatenate the protein embeddings with the positional and strand embeddings. The
        output tensor will have shape [num_proteins, in_dim + 2 * embedding_dim]. The order
        of the concatenation is [protein embeddings, positional embeddings, strand embeddings].

        Args:
            x (torch.Tensor): protein embeddings, shape: [num_proteins, in_dim]
            positional_embed (torch.Tensor): positional embeddings for each protein, shape:
                [num_proteins, embedding_dim]
            strand_embed (torch.Tensor): strand embeddings for each protein, shape:
                [num_proteins, embedding_dim]

        Returns:
            torch.Tensor: concatenated embeddings, shape: [num_proteins, in_dim + 2 * embedding_dim],
                order: [protein embeddings, positional embeddings, strand embeddings]
        """
        x_cat = torch.cat((x, positional_embed, strand_embed), dim=-1)
        return x_cat


class _BaseProteinSetTransformer(
    PositionalStrandEmbeddingModule, Generic[_ModelT, _BaseConfigT]
):
    """Base class for `ProteinSetTransformer` models for either genome-level or protein-level
    tasks. This class sets up either the underlying `SetTransformer` model (genome) or the
    `SetTransformerEncoder` (protein) along with the positional and strand embeddings.

    Further, the `lightning` specific methods for training, validation, and testing are
    implemented by simply calling the `forward` method of this class.

    This is meant to be an abstract base class, so subclasses must implement the following
    methods:

    1. `setup_objective`: to setup the loss function. For loss functions that maintain state,
            like the margin in a triplet loss, a custom model config subclass of `BaseModelConfig`
            that updates the `BaseLossConfig` can be used to add new arguments to this function.
            All fields in the `BaseModelConfig.loss` field are passed to this function.
    2. `forward`: to define the forward pass of the model, including how the loss is computed
    3. `forward_step`: to define how data is passed to the underlying models. This method is
            called by the `databatch_forward` method, which unwraps a custom batch object to pass
            to the model. Presumably, the `forward` method calls the `databatch_forward` or the
            `forward_step` method directly.

    Further, since this is a `lightning.LightningModule`, you can override any of the
    lightning methods such as `training_step`, `validation_step`, `test_step`,
    `configure_optimizers` to further customize functionality.

    WARNING: Subclasses should NOT change the name of the config argument in the __init__ method.
    They should always use `config` as the first argument that is TYPE-HINTED as a subclass of
    `BaseModelConfig`. This is necessary for the `from_pretrained` classmethod to work correctly
    for loading pretrained models.

    This is not meant to be directly subclassed by users. Instead, users should subclass
    `BaseProteinSetTransformer` or `BaseProteinSetTransformerEncoder` for genome-level or
    protein-level tasks. Note: the `BaseProteinSetTransformer` can also be used for dual
    genome and protein-level tasks.
    """

    # keep track of all the valid pretrained model names
    PRETRAINED_MODEL_NAMES = set()

    # NOTE: do not change the name of the config var in all subclasses
    def __init__(self, config: _BaseConfigT, model_type: Type[_ModelT]) -> None:
        # 2048 ptns should be large enough for probably all viruses
        super().__init__(
            in_dim=config.in_dim,
            embed_scale=config.embed_scale,
            max_size=config.max_proteins,
        )
        if config.out_dim == -1:
            config.out_dim = config.in_dim

        self.config = config.model_copy(deep=True)
        # need all new models to set _FIXED_POINTSWAP_RATE to True
        # this way we know that saved models are using the correct sample rate
        self.save_hyperparameters(
            self.config.model_dump(exclude={"fabric"}) | {_FIXED_POINTSWAP_RATE: True}
        )

        self.config.in_dim += self.extra_embedding_dim

        if not config.proj_cat:
            # plm embeddings, positional embeddings, and strand embeddings
            # will be concatenated together and then projected back to the original dim
            # by the first attention layer, BUT if we don't want that
            # then the output dimension will be equal to the original feature dim
            # plus the dim for both the positional and strand embeddings
            self.config.out_dim = self.config.in_dim

        # for genomic PST, the model is a SetTransformer
        # for protein PST, the model is a SetTransformerEncoder
        self.model = self.setup_model(model_type)
        self.is_genomic = isinstance(self.model, SetTransformer)

        self.optimizer_cfg = config.optimizer
        self.augmentation_cfg = config.augmentation
        self.fabric = config.fabric

        self.criterion = self.setup_objective(**config.loss.model_dump())

    def _setup_model(
        self, model_type: Type[_ModelT], include: Optional[set[str]] = None, **kwargs
    ) -> _ModelT:
        model = model_type(**self.config.model_dump(include=include), **kwargs)
        if self.config.compile:
            model: _ModelT = torch.compile(model)  # type: ignore

        return model

    def setup_model(self, model_type: Type[_ModelT]) -> _ModelT:
        # for some reason, typehinting hates if this is part of the if directly
        condition = issubclass(model_type, SetTransformer)

        if condition:
            # genome PST
            include = {
                "in_dim",
                "out_dim",
                "num_heads",
                "n_enc_layers",
                "dropout",
                "layer_dropout",
            }
            kwargs = {}
        else:
            # protein PST
            include = {"in_dim", "out_dim", "num_heads", "dropout", "layer_dropout"}
            kwargs = {"n_layers": self.config.n_enc_layers}

        return self._setup_model(model_type, include=include, **kwargs)

    def setup_objective(
        self, **kwargs
    ) -> torch.nn.Module | Callable[..., torch.Tensor]:
        """**Must be overridden by subclasses to setup the loss function.**

        This function is always passed all fields on the `LossConfig` defined as a subfield of
        the `ModelConfig` used to instantiate this class. For loss functions that maintain
        state, like the margin in a triplet loss, the `LossConfig` (and subsequently the
        `ModelConfig`) can be overridden to add new arguments to this function.

        This funciton should return a callable, such as a `torch.nn.Module`, whose `__call__`
        method computes the loss.
        """
        raise NotImplementedError

    def configure_optimizers(self) -> dict[str, Any]:
        optimizer = torch.optim.AdamW(
            params=self.parameters(),
            lr=self.optimizer_cfg.lr,
            betas=self.optimizer_cfg.betas,
            weight_decay=self.optimizer_cfg.weight_decay,
        )
        config: dict[str, Any] = {"optimizer": optimizer}
        if self.optimizer_cfg.use_scheduler:
            if self.fabric is None:
                self.estimated_steps = self.trainer.estimated_stepping_batches

            scheduler = get_linear_schedule_with_warmup(
                optimizer=optimizer,
                num_warmup_steps=self.optimizer_cfg.warmup_steps,
                num_training_steps=self.estimated_steps,
            )
            config["lr_scheduler"] = {
                "scheduler": scheduler,
                "interval": "epoch",
                "frequency": 1,
            }

        return config

    def check_max_size(self, dataset: GenomeDataset):
        """Checks the maximum number of proteins encoded in a single genome from the dataset.
        The positional embeddings have a maximum size that cannot be easily expanded due to the
        need to readjust the internal graph representations of each genome.

        Args:
            dataset (GenomeDataset): the genome graph dataset
        """
        if dataset.max_size > self.positional_embedding.max_size:
            self.positional_embedding.expand(dataset.max_size)

    @cached_property
    def encoder(self) -> SetTransformerEncoder:
        if isinstance(self.model, SetTransformer):
            return self.model.encoder
        return self.model

    @cached_property
    def decoder(self) -> SetTransformerDecoder:
        if isinstance(self.model, SetTransformer):
            return self.model.decoder
        raise AttributeError("SetTransformerEncoder does not have a decoder")

    def log_loss(self, loss: torch.Tensor, batch_size: int, stage: _STAGE_TYPE):
        """Simple wrapper around the lightning.LightningModule.log method to log the loss."""
        self.log(
            f"{stage}_loss",
            value=loss.item(),
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
            sync_dist=True,
            batch_size=batch_size,
        )

    def training_step(
        self, train_batch: GenomeGraphBatch, batch_idx: int
    ) -> torch.Tensor:
        return self(batch=train_batch, stage="train", augment_data=True)

    def validation_step(
        self, val_batch: GenomeGraphBatch, batch_idx: int
    ) -> torch.Tensor:
        return self(batch=val_batch, stage="val", augment_data=False)

    def test_step(self, test_batch: GenomeGraphBatch, batch_idx: int) -> torch.Tensor:
        return self(batch=test_batch, stage="test", augment_data=False)

    def _databatch_forward_with_embeddings(
        self, batch: GenomeGraphBatch, return_attention_weights: bool = True
    ) -> GraphAttnOutput | EdgeAttnOutput:
        x_with_pos_and_strand, _, _ = self.internal_embeddings(batch)

        return self.databatch_forward(
            batch=batch,
            x=x_with_pos_and_strand,
            return_attention_weights=return_attention_weights,
        )

    def predict_step(
        self, batch: GenomeGraphBatch, batch_idx: int, dataloader_idx: int = 0
    ) -> GraphAttnOutput | EdgeAttnOutput:
        return self._databatch_forward_with_embeddings(
            batch=batch, return_attention_weights=True
        )

    def forward_step(
        self,
        x: torch.Tensor,
        edge_index: torch.Tensor,
        ptr: torch.Tensor,
        batch: OptTensor = None,
        return_attention_weights: bool = False,
    ) -> GraphAttnOutput | EdgeAttnOutput:
        """**Must be overridden by subclasses to define how the model's direct forward pass
        uses this these arguments.**

        This is a simple wrapper around the model's forward method used simply for computing
        the embeddings.

        This method should be called by the `.forward` method. Additionally, the specific data
        handling steps to compute the loss should be handled by the `.forward` method.

        These arguments are used by the graph data model of PyTorch Geometric.

        Args:
            x (torch.Tensor): shape [num_proteins, in_dim]; the protein embeddings
            edge_index (torch.Tensor): shape (2, num_edges); the edge index tensor that connects
                protein nodes in genome subgraphs
            ptr (torch.Tensor): shape (num_proteins + 1,); the index pointer tensor for efficient
                random access to the protein embeddings from each genome. Only needed for genome
                level implementations, such as with the decoder of the `SetTransformer`.
            batch (Optional[torch.Tensor]): shape (num_proteins,); the batch tensor that assigns
                each protein to a specific genome (graph). This can be computed from the `ptr`
                tensor, so it is optional, but passing a precomputed batch tensor will be more
                efficient.
            return_attention_weights (bool): whether to return the attention weights, probably
                only wanted for debugging or for final predictions

        Returns:
            NamedTuples:
                - GraphAttnOutput: IF this is a genomic PST (using the `SetTransformer` internally),
                    the output includes fields (out, attn) where `out` is the genome embeddings
                    and `attn` is the attention weights (shape: [num_genomes, num_heads]). The
                    attention weights are only returned if `return_attention_weights` is True.
                - EdgeAttnOutput: IF this is a protein PST (using the `SetTransformerEncoder`
                    internally), the output includes fields (out, edge_index, attn) where `out`
                    is the protein embeddings, `edge_index` is the edge index tensor used for
                    the message passing attention calculation, and `attn` is the attention
                    weights (shape: [num_edges, num_heads]). The attention weights are only
                    returned if `return_attention_weights` is True. NOTE: the `edge_index`
                    tensor is not necessarily the same as the one that was input since the
                    model internally adds self loops if they are not present. `attn` is computed
                    over the edges in the **returned** `edge_index`.
        """
        raise NotImplementedError

    def databatch_forward(
        self,
        batch: GenomeGraphBatch,
        return_attention_weights: bool = False,
        x: OptTensor = None,
    ) -> GraphAttnOutput | EdgeAttnOutput:
        """Calls the forward method of the underlying `SetTransformer` or `SetTransformerEncoder`
        model by unwrapping the `GenomeGraphBatch` fields.

        Args:
            batch (GenomeGraphBatch): The batch object containing the protein embeddings,
                edge index, the index pointer, the number of proteins in each genome, etc, that
                are used for the forward pass of the `SetTransformer` or `SetTransformerEncoder`.
                This object models the data patterns of PyTorch Geometric graphs.
            return_attention_weights (bool): whether to return the attention weights, probably
                only wanted for debugging or for final predictions
            x (Optional[torch.Tensor]): Used to allow custom protein embeddings, such as those
                that include positional and strand embeddings, to be passed to the model. If
                `x` is not provided, the model will use the raw protein embeddings in the batch
                object. This is useful when both the raw protein embeddings and modified
                embeddings are needed for the forward pass.

        Returns:
            NamedTuples:
                - GraphAttnOutput: IF this is a genomic PST (using the `SetTransformer` internally),
                    the output includes fields (out, attn) where `out` is the genome embeddings
                    and `attn` is the attention weights. The attention weights are only returned
                    if `return_attention_weights` is True.
                - EdgeAttnOutput: IF this is a protein PST (using the `SetTransformerEncoder`
                    internally), the output includes fields (out, edge_index, attn) where `out`
                    is the protein embeddings, `edge_index` is the edge index tensor used for
                    the message passing attention calculation, and `attn` is the attention
                    weights. The attention weights are only returned if `return_attention_weights`
                    is True. NOTE: the `edge_index` tensor is not necessarily the same as the
                    one that was input since the model internally adds self loops if they are
                    not present.
        """
        if x is None:
            x = batch.x

        return self.forward_step(
            x=x,
            edge_index=batch.edge_index,
            ptr=batch.ptr,
            batch=batch.batch,
            return_attention_weights=return_attention_weights,
        )

    def forward(self, batch: GenomeGraphBatch, stage: _STAGE_TYPE, **kwargs):
        """**Must be overridden by subclasses to define the forward pass.**

        Implement the specific steps for the forward pass. For example, if using a triplet
        loss objective, this method should compute the genome embeddings by calling the
        `.forward_step` method, then triplet sampling, and then computing the loss.

        An optional but recommended method to call instead of `.forward_step` is
        `.databatch_forward`, which can take the batch object itself, and unwrap the arguments
        to `.forward_step`.

        Args:
            batch (GenomeGraphBatch): The batch object containing the protein embeddings,
                edge index, the index pointer, the number of proteins in each genome, etc, that
                are used for the forward pass of the `SetTransformer`. This object models the
                data patterns of PyTorch Geometric graphs.
            stage (_STAGE_TYPE): The stage of training, validation, or testing.
                Choices = ["train", "val", "test"]
            **kwargs: Additional keyword arguments that can be passed to the forward pass.
        """
        raise NotImplementedError

    @classmethod
    def _resolve_model_config_type(cls) -> Type[_BaseConfigT]:
        # try to get the model config type from the type annotation in the __init__ method
        type_hints = get_type_hints(cls.__init__)

        if "config" not in type_hints:
            # no type hint for the config parameter - just use default
            model_config_type = BaseModelConfig
        else:
            # get the type hint for the config parameter
            model_config_type = type_hints["config"]

            # for these base classes, the type hint is a TypeVar, so check that
            if issubclass(type(model_config_type), TypeVar):
                # the type hint is a TypeVar, so just use the default
                model_config_type = BaseModelConfig

        return cast(Type[_BaseConfigT], model_config_type)

    def _try_load_state_dict(
        self, state_dict: dict[str, torch.Tensor], strict: bool = True
    ):
        try:
            # just try to load directly
            self.load_state_dict(state_dict, strict=True)
        except RuntimeError:
            # if that fails, then we need to try to see if we are loading a pretrained model
            # that does not have values for new layers in subclassed models

            # get the base parameters of the SetTransformer or SetTransformerEncoder
            # along with the positional and strand embeddings
            base_params = {f"model.{name}" for name, _ in self.model.named_parameters()}

            # PositionalStrandEmbeddingModuleMixin params
            for embedding_name in ("positional_embedding", "strand_embedding"):
                layer: torch.nn.Module = getattr(self, embedding_name)
                for name, _ in layer.named_parameters():
                    base_params.add(f"{embedding_name}.{name}")

            # get all new params
            current_params = {name for name, _ in self.named_parameters()}

            new_params = current_params - base_params

            # now try to load the state dict
            missing, unexpected = map(
                set, self.load_state_dict(state_dict, strict=False)
            )

            # missing should be equivalent to the new params if loaded correctly
            still_missing = new_params - missing

            if still_missing:
                raise RuntimeError(
                    f"Missing parameters: {still_missing} when loading the state dict"
                )

            if strict and unexpected:
                raise RuntimeError(
                    f"Unexpected parameters: {unexpected} when loading the state dict"
                )

    @staticmethod
    def _adjust_checkpoint_inplace(ckpt: dict[str, Any]):
        # there was an old error when computing the pointswap rate to be 1 - expected
        # the code has been changed (see commit 82b0698)
        # however, old checkpoints will have the previous value, which needs to be adjusted
        if ckpt["hyper_parameters"].pop(_FIXED_POINTSWAP_RATE, None) is None:
            # key not present = old model
            # so need to adjust the sample rate
            curr_rate = ckpt["hyper_parameters"]["augmentation"]["sample_rate"]
            ckpt["hyper_parameters"]["augmentation"]["sample_rate"] = 1.0 - curr_rate

    @classmethod
    def from_pretrained(
        cls,
        pretrained_model_name_or_path: str | Path,
        model_type: Type[_ModelT],
        model_config_type: Optional[Type[_BaseConfigT]] = None,
        strict: bool = True,
    ) -> Self:
        """Load a model from a pretrained (or just trained) checkpoint.

        This method handles subclasses that add new trainable parameters that want to start
        from a parent pretrained model. For example, subclassing a `ProteinSetTransformer` model
        and changing the objective from triplet loss to some label classification would introduce
        new trainable layers that would benefit from starting from a pretrained PST.

        This method works with allows interchanging between `SetTransformer` (genomic) and
        `SetTransformerEncoder` (protein) models. The most common case of this would be with
        a pretrained genomic PST that is then finetuned for a protein-level task in which
        the decoder of the `SetTransformer` is not used.

        Args:
            pretrained_model_name_or_path (str | Path): name or file path to the pretrained model
                checkpoint. NOTE: passing model names is not currently supported
            model_type (Type[_ModelT]): underlying model type. For protein-level tasks, this
                should be `SetTransformerEncoder`. For genome-level tasks, this should be
                `SetTransformer`. It is recommended that subclasses handle this so users do not
                have to pass this argument.
            model_config_type (Optional[Type[_BaseConfigT]], optional): the model config
                pydantic model. If passed, this must be a subclass of `BaseModelConfig`.
                Defaults to None, meaning that it will be auto-detected from the class's
                __init__ type hinted signature or default to `BaseModelConfig` if the
                type annotation cannot be detected. NOTE: it is recommended that subclasses
                type hint the `config` argument to the __init__ method to ensure that the
                type of the model config is correctly detected.
            strict (bool, optional): raise a RuntimeError if there are unexpected parameters
                in the checkpoint's state dict. Defaults to True.

        Raises:
            NotImplementedError: Loading models from their names is not implemented yet
        """
        if not isinstance(pretrained_model_name_or_path, Path):
            # could either be a str path or a model name
            valid_model_names = cls.PRETRAINED_MODEL_NAMES

            if pretrained_model_name_or_path in valid_model_names:
                # load from external source
                # TODO: can call download code written in this module...
                raise NotImplementedError(
                    "Loading from external source not implemented yet"
                )
            else:
                # assume str is a file path
                pretrained_model_name_or_path = Path(pretrained_model_name_or_path)

        if model_config_type is None:
            model_config_type = cls._resolve_model_config_type()

        ckpt = torch.load(pretrained_model_name_or_path, map_location="cpu")
        cls._adjust_checkpoint_inplace(ckpt)
        model_config = model_config_type.model_validate(ckpt["hyper_parameters"])

        try:
            model = cls(config=model_config, model_type=model_type)
        except TypeError:
            # subclasses may not have the model_type as it will probably be set
            model = cls(config=model_config)  # type: ignore

        model._try_load_state_dict(ckpt["state_dict"], strict=strict)

        return model


class BaseProteinSetTransformer(
    _BaseProteinSetTransformer[SetTransformer, _BaseConfigT]
):
    """Base class for a genome-level `ProteinSetTransformer` model. This class sets up the
    the underlying `SetTransformer` model along with the positional and strand embeddings.

    This is an abstract base class, so subclasses must implement the following methods:
    1. `setup_objective`: to setup the loss function
    2. `forward`: to define the forward pass of the model, including how the loss is computed

    If the loss function requires additional parameters, a custom model config subclass of `BaseModelConfig` can be used that replaces the `BaseLossConfig` with new fields.
    """

    MODEL_TYPE = SetTransformer

    def __init__(self, config: _BaseConfigT):
        super().__init__(config=config, model_type=self.MODEL_TYPE)

    # change type annotations in the signature
    def databatch_forward(
        self,
        batch: GenomeGraphBatch,
        return_attention_weights: bool = False,
        x: OptTensor = None,
    ) -> GraphAttnOutput:
        result = super().databatch_forward(
            batch=batch, return_attention_weights=return_attention_weights, x=x
        )

        return cast(GraphAttnOutput, result)

    def predict_step(
        self, batch: GenomeGraphBatch, batch_idx: int, dataloader_idx: int = 0
    ) -> GraphAttnOutput:
        result = super().predict_step(
            batch=batch, batch_idx=batch_idx, dataloader_idx=dataloader_idx
        )

        return cast(GraphAttnOutput, result)

    def forward_step(
        self,
        x: torch.Tensor,
        edge_index: torch.Tensor,
        ptr: torch.Tensor,
        batch: OptTensor = None,
        return_attention_weights: bool = False,
    ) -> GraphAttnOutput:
        # GraphAttnOutput
        output: GraphAttnOutput = self.model(
            x=x,
            edge_index=edge_index,
            ptr=ptr,
            batch=batch,
            return_attention_weights=return_attention_weights,
        )

        return output

    @classmethod
    def from_pretrained(
        cls, pretrained_model_name_or_path: str | Path, strict: bool = True
    ) -> Self:
        """Load a model from a pretrained (or just trained) checkpoint.

        This method handles subclasses that add new trainable parameters that want to start
        from a parent pretrained model. For example, subclassing a `ProteinSetTransformer` model
        and changing the objective from triplet loss to some label classification would introduce
        new trainable layers that would benefit from starting from a pretrained PST.

        This method works with allows interchanging between `SetTransformer` (genomic) and
        `SetTransformerEncoder` (protein) models. The most common case of this would be with
        a pretrained genomic PST that is then finetuned for a protein-level task in which
        the decoder of the `SetTransformer` is not used.

        Args:
            pretrained_model_name_or_path (str | Path): name or file path to the pretrained model
                checkpoint. NOTE: passing model names is not currently supported
            strict (bool, optional): raise a RuntimeError if there are unexpected parameters
                in the checkpoint's state dict. Defaults to True.

        Raises:
            NotImplementedError: Loading models from their names is not implemented yet
        """
        return super().from_pretrained(
            pretrained_model_name_or_path, model_type=cls.MODEL_TYPE, strict=strict
        )


class ProteinSetTransformer(BaseProteinSetTransformer[ModelConfig]):
    # NOTE: updated as new pretrained models are added
    PRETRAINED_MODEL_NAMES = {"vpst-small", "vpst-large"}

    def __init__(self, config: ModelConfig):
        # this only needs to be defined for the config type hint
        super().__init__(config=config)

    def setup_objective(self, margin: float, **kwargs) -> AugmentedWeightedTripletLoss:
        return AugmentedWeightedTripletLoss(margin=margin)

    def forward(
        self,
        batch: GenomeGraphBatch,
        stage: _STAGE_TYPE,
        augment_data: bool = True,
        **kwargs,
    ) -> torch.Tensor:
        """Forward pass using Point Swap augmentation (during training only) with a triplet loss function."""

        # adding positional and strand embeddings lead to those dominating the plm signal
        # we can concatenate them here, then use a linear layer to project down back to
        # the original feature dim and force the model to directly learn which of these
        # are most important

        # NOTE: we do not adjust the original data at batch.x
        # this lets the augmented data adjust the positional and strand embeddings
        # independently of the original data
        x, positional_embed, strand_embed = self.internal_embeddings(batch)

        # calculate chamfer distance only based on the plm embeddings
        # want to maximize that signal over strand and positional embeddings
        batch_size = batch.num_proteins.numel()
        setwise_dist, item_flow = stacked_batch_chamfer_distance(
            batch=batch.x, ptr=batch.ptr
        )
        setwise_dist_std = setwise_dist.std()

        #### REAL DATA ####
        # positive mining
        pos_idx = positive_sampling(setwise_dist)

        # forward pass
        y_anchor, _ = self.databatch_forward(
            batch=batch,
            return_attention_weights=False,
            x=x,
        )

        # negative sampling
        neg_idx, neg_weights = negative_sampling(
            input_space_pairwise_dist=setwise_dist,
            output_embed_X=y_anchor,
            input_space_dist_std=setwise_dist_std,
            pos_idx=pos_idx,
            scale=self.config.loss.sample_scale,
            no_negatives_mode=self.config.loss.no_negatives_mode,
        )

        y_pos = y_anchor[pos_idx]
        y_neg = y_anchor[neg_idx]

        if augment_data:
            y_aug_pos, y_aug_neg, aug_neg_weights = self._augmented_forward_step(
                batch=batch,
                pos_idx=pos_idx,
                y_anchor=y_anchor,
                item_flow=item_flow,
                positional_embed=positional_embed,
            )
        else:
            y_aug_pos = None
            y_aug_neg = None
            aug_neg_weights = None

        loss: torch.Tensor = self.criterion(
            y_self=y_anchor,
            y_pos=y_pos,
            y_neg=y_neg,
            neg_weights=neg_weights,
            class_weights=batch.weight,  # type: ignore
            y_aug_pos=y_aug_pos,
            y_aug_neg=y_aug_neg,
            aug_neg_weights=aug_neg_weights,
        )

        if self.fabric is None:
            self.log_loss(loss=loss, batch_size=batch_size, stage=stage)
        return loss

    def _augmented_forward_step(
        self,
        batch: GenomeGraphBatch,
        pos_idx: torch.Tensor,
        y_anchor: torch.Tensor,
        item_flow: torch.Tensor,
        positional_embed: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        augmented_batch, aug_idx = point_swap_sampling(
            batch=batch.x,
            pos_idx=pos_idx,
            item_flow=item_flow,
            sizes=batch.num_proteins,
            sample_rate=self.augmentation_cfg.sample_rate,
        )

        # let strand use original strand for each ptn
        strand = batch.strand[aug_idx]
        strand_embed = self.strand_embedding(strand)

        # however instead of changing the positional idx, just keep the same
        # this is basically attempting to mirror same protein encoded in a diff position
        x_aug = self.concatenate_embeddings(
            x=augmented_batch,
            positional_embed=positional_embed,
            strand_embed=strand_embed,
        )

        y_aug_pos, _ = self.databatch_forward(
            batch=batch,
            return_attention_weights=False,
            x=x_aug,
        )

        # NOTE: computing chamfer distance without positional or strand info
        setdist_real_aug, _ = stacked_batch_chamfer_distance(
            batch=batch.x, ptr=batch.ptr, other=augmented_batch
        )

        aug_neg_idx, aug_neg_weights = negative_sampling(
            input_space_pairwise_dist=setdist_real_aug,
            output_embed_X=y_anchor,
            output_embed_Y=y_aug_pos,
            scale=self.config.loss.sample_scale,
            no_negatives_mode=self.config.loss.no_negatives_mode,
        )

        y_aug_neg = y_aug_pos[aug_neg_idx]
        return y_aug_pos, y_aug_neg, aug_neg_weights

    @staticmethod
    def _adjust_checkpoint_inplace(ckpt: dict[str, Any]):
        # fix the sample rate issue first
        _BaseProteinSetTransformer._adjust_checkpoint_inplace(ckpt)

        # move sample_scale and no_negatives_mode to the loss field
        # if they are part of the augmentation field
        hparams = ckpt["hyper_parameters"]
        for hparam_name in ("sample_scale", "no_negatives_mode"):
            if hparam_name in hparams["augmentation"]:
                hparams["loss"][hparam_name] = hparams["augmentation"].pop(hparam_name)


class CrossValPST(CrossValModuleMixin, ProteinSetTransformer):
    __error_msg__ = (
        "Model {stage} is not allowed during cross validation. Only training and "
        "validation is supported."
    )

    def __init__(self, config: ModelConfig):
        ProteinSetTransformer.__init__(self, config=config)

        # needed for type hints
        self.fabric: L.Fabric
        CrossValModuleMixin.__init__(self, config=config)

    def test_step(self, test_batch: GenomeGraphBatch, batch_idx: int):
        raise RuntimeError(self.__error_msg__.format(stage="testing"))

    def predict_step(
        self, batch: GenomeGraphBatch, batch_idx: int, dataloader_idx: int = 0
    ):
        raise RuntimeError(self.__error_msg__.format(stage="inference"))


class BaseProteinSetTransformerEncoder(
    _BaseProteinSetTransformer[SetTransformerEncoder, _BaseConfigT],
):
    """Base class for protein-level tasks using the `SetTransformerEncoder` model. This class
    can also be derived from pretrained genome-level models, such as the `ProteinSetTransformer`.
    The genome decoding layers from pretrained models are dropped, leaving on the encoder layers
    and the positional and strand embeddings.

    This class is meant to be an abstract base class, so subclasses must implement the following
    methods:

    1. `setup_objective`: to setup the loss function
    2. `forward`: to define the forward pass of the model, including how the loss is computed
    """

    MODEL_TYPE = SetTransformerEncoder

    def __init__(self, config: _BaseConfigT):
        super().__init__(config=config, model_type=self.MODEL_TYPE)

    @classmethod
    def from_pretrained(
        cls, pretrained_model_name_or_path: str | Path, strict: bool = True
    ) -> Self:
        """Load a model from a pretrained (or just trained) checkpoint.

        This method handles subclasses that add new trainable parameters that want to start
        from a parent pretrained model. For example, subclassing a `ProteinSetTransformer` model
        and changing the objective from triplet loss to some label classification would introduce
        new trainable layers that would benefit from starting from a pretrained PST.

        This method works with allows interchanging between `SetTransformer` (genomic) and
        `SetTransformerEncoder` (protein) models. The most common case of this would be with
        a pretrained genomic PST that is then finetuned for a protein-level task in which
        the decoder of the `SetTransformer` is not used.

        Args:
            pretrained_model_name_or_path (str | Path): name or file path to the pretrained model
                checkpoint. NOTE: passing model names is not currently supported
            strict (bool, optional): raise a RuntimeError if there are unexpected parameters
                in the checkpoint's state dict. Defaults to True.

        Raises:
            NotImplementedError: Loading models from their names is not implemented yet
        """
        return super().from_pretrained(
            pretrained_model_name_or_path, model_type=cls.MODEL_TYPE, strict=strict
        )

    @staticmethod
    def _adjust_checkpoint_inplace(ckpt: dict[str, Any]):
        # fix augmentation sample rate and move sample_scale and no_negatives_mode to loss field
        ProteinSetTransformer._adjust_checkpoint_inplace(ckpt)

        # since we could be loading a genomic PST for this protein PST,
        # then we need to remove the decoder layers

    def _try_load_state_dict(
        self, state_dict: dict[str, torch.Tensor], strict: bool = True
    ):

        try:
            # try loading directly, which should work for pretrained protein PSTs
            # derived from this class
            super()._try_load_state_dict(state_dict, strict=strict)
        except RuntimeError:
            # however, if it fails, it is likely that the ckpt is from a genomic PST
            # so we need to extract the pos/strand embeddings and the SetTransformerEncoder
            # state dict ONLY
            # we do not care about the SetTransformerDecoder state dict!!

            # we just need to rebuild the state dict to only include the relevant layers

            # get the base parameters of the SetTransformerEncoder from the view of a
            # ProteinSetTransformer: ie from PST, the encoder is under the field model.encoder.AAA
            # also get with the positional and strand embeddings
            params_to_extract = {
                f"model.encoder.{name}" for name, _ in self.model.named_parameters()
            }

            # PositionalStrandEmbeddingModuleMixin params
            for embedding_name in ("positional_embedding", "strand_embedding"):
                layer: torch.nn.Module = getattr(self, embedding_name)
                for name, _ in layer.named_parameters():
                    params_to_extract.add(f"{embedding_name}.{name}")

            # from PST, the encoder is under the field model.encoder.AAA
            # but for the protein PST, the expected field is model.AAA
            new_state_dict = {
                name.replace("encoder.", ""): state_dict[name]
                for name in params_to_extract
            }

            # get all new params
            current_params = {name for name, _ in self.named_parameters()}

            new_params = current_params - new_state_dict.keys()

            # now try to load the state dict
            missing, unexpected = map(
                set, self.load_state_dict(new_state_dict, strict=False)
            )

            # missing should be equivalent to the new params if loaded correctly
            still_missing = new_params - missing

            if still_missing:
                raise RuntimeError(
                    f"Missing parameters: {still_missing} when loading the state dict"
                )

            if strict and unexpected:
                raise RuntimeError(
                    f"Unexpected parameters: {unexpected} when loading the state dict"
                )

    def forward_step(
        self,
        x: torch.Tensor,
        edge_index: torch.Tensor,
        ptr: torch.Tensor,
        batch: OptTensor = None,
        return_attention_weights: bool = False,
    ) -> EdgeAttnOutput:
        # ptr is not used here but needed for the signature due to inheritance
        output: EdgeAttnOutput = self.model(
            x=x,
            edge_index=edge_index,
            batch=batch,
            return_attention_weights=return_attention_weights,
        )

        return output

    # change type annotations
    def databatch_forward(
        self,
        batch: GenomeGraphBatch,
        return_attention_weights: bool = False,
        x: OptTensor = None,
    ) -> EdgeAttnOutput:
        result = super().databatch_forward(
            batch=batch, return_attention_weights=return_attention_weights, x=x
        )

        return cast(EdgeAttnOutput, result)

    def predict_step(
        self, batch: GenomeGraphBatch, batch_idx: int, dataloader_idx: int = 0
    ) -> EdgeAttnOutput:
        result = super().predict_step(
            batch=batch, batch_idx=batch_idx, dataloader_idx=dataloader_idx
        )

        return cast(EdgeAttnOutput, result)


class ProteinSetTransformerEncoder(BaseProteinSetTransformerEncoder):
    def __init__(self, config: ModelConfig):
        super().__init__(config=config)

        self.config = cast(ModelConfig, self.config)

    def setup_objective(self, margin: float, **kwargs) -> WeightedTripletLoss:
        return WeightedTripletLoss(margin=margin)

    def forward(
        self, batch: GenomeGraphBatch, stage: _STAGE_TYPE, **kwargs
    ) -> torch.Tensor:
        # adding positional and strand embeddings lead to those dominating the plm signal
        # so we concatenate them here

        # NOTE: we do not adjust the original data at batch.x since we need that
        # for triplet sampling
        x, _, _ = self.internal_embeddings(batch)

        # calculate distances only based on the plm embeddings
        # want to maximize that signal over strand and positional embeddings
        all_pairwise_dist = pairwise_euclidean_distance(batch.x)
        all_pairwise_dist_std = all_pairwise_dist.std()

        ### positive sampling -
        # happens in input pLM embedding space
        # need to set the diagonal to inf to avoid selecting the same protein as the positive example
        pos_idx = positive_sampling(all_pairwise_dist)

        ### semihard negative sampling -
        # choose the negative example that is closest to positive
        # and farther from the anchor than the positive example
        # NOTE: happens in PST contextualized protein embedding space, ie negative examples
        # are chosen dynamically as model updates

        # forward pass -> ctx ptn [P, D]
        y_anchor, _, _ = self.databatch_forward(
            batch=batch,
            return_attention_weights=False,
            x=x,
        )

        neg_idx, neg_weights = negative_sampling(
            input_space_pairwise_dist=all_pairwise_dist,
            output_embed_X=y_anchor,
            input_space_dist_std=all_pairwise_dist_std,
            pos_idx=pos_idx,
            scale=self.config.loss.sample_scale,
        )

        y_pos = y_anchor[pos_idx]
        y_neg = y_anchor[neg_idx]

        loss: torch.Tensor = self.criterion(
            y_self=y_anchor,
            y_pos=y_pos,
            y_neg=y_neg,
            weights=neg_weights,
            class_weights=None,
            reduce=True,
        )

        batch_size = batch.num_proteins.numel()
        if self.fabric is None:
            self.log_loss(loss=loss, batch_size=batch_size, stage=stage)
        return loss
