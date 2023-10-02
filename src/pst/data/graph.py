from __future__ import annotations

from functools import partial
from itertools import permutations
from typing import Callable

import torch
from more_itertools import chunked
from torch_geometric.data import Data
from torch_geometric.typing import OptTensor

from pst.typing import EdgeIndexStrategy

_DEFAULT_EDGE_STRATEGY = "chunked"
_DEFAULT_CHUNK_SIZE = 30
_SENTINEL_THRESHOLD = -1
_DEFAULT_THRESHOLD = 30

EdgeIndexCreateFn = Callable[..., torch.Tensor]


class GenomeGraph(Data):
    x: torch.Tensor
    edge_index: torch.Tensor
    y: OptTensor
    pos: torch.Tensor
    num_proteins: int
    class_id: int
    strand: torch.Tensor
    weight: float

    def __init__(
        self,
        x: torch.Tensor,
        class_id: int,
        strand: torch.Tensor,
        weight: float,
        num_proteins: int,
        pos: torch.Tensor,
        edge_index: OptTensor = None,
        edge_attr: OptTensor = None,
        y: OptTensor = None,
        edge_strategy: EdgeIndexStrategy = _DEFAULT_EDGE_STRATEGY,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        threshold: int = _SENTINEL_THRESHOLD,
        **kwargs,
    ):
        kwargs["num_proteins"] = num_proteins
        kwargs["class_id"] = class_id
        kwargs["strand"] = strand
        kwargs["weight"] = weight

        # this will set all attrs for this subclass
        super().__init__(x, edge_index, edge_attr, y, pos, **kwargs)

        self._edge_strategy: EdgeIndexStrategy = edge_strategy
        self._chunk_size = chunk_size
        self._threshold = threshold

        # can't do this with pyg Batch dynamic inheritance
        # if self.edge_index is None:
        #     self.edge_index = GenomeGraph.create_edge_index(
        #         self.x.size(0), edge_strategy, chunk_size, threshold
        #     )

    def set_edge_index(self, *, override: bool = False):
        if not override and self.edge_index is not None:
            return

        self.edge_index = self.create_edge_index(
            num_nodes=self.num_proteins,
            edge_strategy=self._edge_strategy,
            chunk_size=self._chunk_size,
            threshold=self._threshold,
        )

    @staticmethod
    def create_fully_connected_graph(num_nodes: int) -> torch.Tensor:
        edge_index = (
            torch.tensor(list(permutations(range(num_nodes), r=2))).t().contiguous()
        )
        return edge_index

    @staticmethod
    def filter_edges_by_seq_distance(
        edge_index: torch.Tensor, threshold: int
    ) -> torch.Tensor:
        distance = torch.abs(edge_index[0] - edge_index[1])
        local_edge_index = edge_index[:, distance <= threshold].contiguous()
        return local_edge_index

    @staticmethod
    def create_sparse_graph(num_nodes: int, threshold: int) -> torch.Tensor:
        edge_index = GenomeGraph.create_fully_connected_graph(num_nodes)
        edge_index = GenomeGraph.filter_edges_by_seq_distance(edge_index, threshold)
        return edge_index

    @staticmethod
    def create_chunked_graph(
        num_nodes: int, chunk_size: int, threshold: int = _SENTINEL_THRESHOLD
    ) -> torch.Tensor:
        connected_comp = list(chunked(range(num_nodes), n=chunk_size))
        # don't want any connected components / subgraphs that only have 1 node
        if len(connected_comp[-1]) == 1:
            connected_comp[-2].extend(connected_comp[-1])
            del connected_comp[-1]

        # False if threshold == -1 or >= chunk_size
        # True if threshold < chunk_size
        filter_edges = not (threshold == _SENTINEL_THRESHOLD or threshold >= chunk_size)

        _edge_index: list[torch.Tensor] = list()
        offset = 0
        for cc in connected_comp:
            cc_num_nodes = len(cc)
            edges = GenomeGraph.create_fully_connected_graph(cc_num_nodes) + offset
            if filter_edges:
                edges = GenomeGraph.filter_edges_by_seq_distance(edges, threshold)
            _edge_index.append(edges)
            offset += cc_num_nodes

        edge_index = torch.cat(_edge_index, dim=1)
        return edge_index

    @staticmethod
    def _edge_index_create_method(
        edge_strategy: EdgeIndexStrategy, chunk_size: int, threshold: int
    ) -> EdgeIndexCreateFn:
        kwargs = dict()
        if edge_strategy == "sparse":
            if threshold <= 1:
                errmsg = (
                    f"Passed {edge_strategy=}, which requires the `threshold`"
                    " arg for `create_sparse_graph` to be >1"
                )
                raise ValueError(errmsg)
            kwargs["threshold"] = threshold
            edge_create_fn = GenomeGraph.create_sparse_graph
        elif edge_strategy == "chunked":
            kwargs["threshold"] = threshold
            kwargs["chunk_size"] = chunk_size
            edge_create_fn = GenomeGraph.create_chunked_graph
        else:
            edge_create_fn = GenomeGraph.create_fully_connected_graph

        return partial(edge_create_fn, **kwargs)

    @staticmethod
    def create_edge_index(
        num_nodes: int,
        edge_strategy: EdgeIndexStrategy,
        chunk_size: int,
        threshold: int,
    ) -> torch.Tensor:
        edge_create_fn = GenomeGraph._edge_index_create_method(
            edge_strategy=edge_strategy, chunk_size=chunk_size, threshold=threshold
        )

        edge_index = edge_create_fn(num_nodes=num_nodes)
        return edge_index
