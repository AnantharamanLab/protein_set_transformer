from __future__ import annotations

from functools import partial
from pathlib import Path

import lightning_cv as lcv
import numpy as np
import optuna
import tables as tb

from pst.arch import GenomeDataModule
from pst.arch.modules import CrossValPST as PST
from pst.utils.cli import TrainingMode


def _optimize(trial: optuna.Trial, tuner: lcv.tuning.Tuner) -> float:
    return tuner.tune(trial=trial)


def _peek_feature_dim(data_file: Path) -> int:
    with tb.File(data_file) as fp:
        return np.shape(fp.root.data[0])[-1]


def tune(config: TrainingMode):
    pruner: optuna.pruners.BasePruner = (
        optuna.pruners.MedianPruner(
            n_startup_trials=1,  # only need to finish 1 trial before pruning
            n_warmup_steps=5,  # wait 5 epochs before trying to prune
        )
        if config.experiment.prune
        else optuna.pruners.NopPruner()
    )

    # update model's in_dim
    if config.model.in_dim == -1:
        config.model.in_dim = _peek_feature_dim(config.data.file)

    trainer_callbacks: list[lcv.callbacks.Callback] = [
        lcv.callbacks.ModelCheckpoint(
            save_last=True, save_top_k=config.experiment.save_top_k, every_n_epochs=1
        )
    ]

    if config.model.optimizer.use_scheduler:
        trainer_callbacks.append(lcv.callbacks.LearningRateMonitor())

    trainer_config = lcv.CrossValidationTrainerConfig(
        loggers=None,  # defaults to CSVLogger THRU TUNER
        callbacks=trainer_callbacks,
        limit_train_batches=config.trainer.limit_train_batches or 1.0,
        limit_val_batches=config.trainer.limit_val_batches or 1.0,
        checkpoint_dir=config.trainer.default_root_dir,
        # everything else is the same name
        **config.trainer.model_dump(
            include={"accelerator", "strategy", "devices", "precision", "max_epochs"}
        ),
    )

    tuner = lcv.tuning.Tuner(
        model_type=PST,  # type: ignore
        model_config=config.model,
        datamodule_type=GenomeDataModule,
        datamodule_config=config.data,
        trainer_config=trainer_config,
        logdir=config.trainer.default_root_dir,
        experiment_name=config.experiment.name,
        hparam_config_file=config.experiment.config,
    )

    study = optuna.create_study(
        # storage=
        direction="minimize",
        study_name=config.experiment.name,
        pruner=pruner,
        load_if_exists=True,
    )

    optimize = partial(_optimize, tuner=tuner)
    optuna_callbacks = [
        lcv.tuning.callbacks.OptunaHyperparameterLogger(
            root_dir=config.trainer.default_root_dir
        )
    ]
    study.optimize(
        func=optimize,
        n_trials=config.experiment.n_trials,
        callbacks=optuna_callbacks,
        gc_after_trial=True,
    )
