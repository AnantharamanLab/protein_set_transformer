from __future__ import annotations

import os
from functools import partial
from multiprocessing import Manager, Queue
from pathlib import Path
from typing import Optional

import lightning as L
import optuna
import torch
from joblib import parallel_backend
from optuna.integration.pytorch_lightning import PyTorchLightningPruningCallback

from pst.utils import LIGHTNING_VERSION_GTE_2

from .hyperparameters import HyperparameterRegistryMixin
from .optuna_callbacks import (
    OptunaHyperparameterLogger,
    OptunaPruningLightningCallback,
)
from .train import CrossValidationTrainer


def _get_logdir(default_root_dir: Path, exp_name: str) -> Path:
    return default_root_dir.joinpath(exp_name)


# TODO: add configfile as param
class TuningCrossValidationTrainer(CrossValidationTrainer, HyperparameterRegistryMixin):
    def __init__(
        self,
        trial: optuna.Trial,
        configfile: Optional[Path] = None,
        **cv_trainer_kwargs,
    ):
        HyperparameterRegistryMixin.__init__(self, trial=trial, configfile=configfile)
        trial_number = f"TRIAL {self._trial.number}"
        self._trial_status = partial(
            "==============={trial} {status}===============".format, trial=trial_number
        )

        ### Sample hyperparamters ###
        # this is needed to add any args that are conditional switches
        # to the self._hparams attribute

        # TODO: do the cv_trainer_kwargs need to be copied so that they aren't shared?
        # idk
        self.add_switches(**cv_trainer_kwargs)
        self.register_hparams()

        cv_trainer_kwargs.update(self._hparams)

        self.cv_trainer_kwargs = cv_trainer_kwargs

    def add_switches(self, **kwargs):
        switch_args = ["swa"]  # TODO: make this all more programmatic
        for arg in switch_args:
            self._hparams[arg] = kwargs[arg]

    def on_train_with_cross_validation_start(self):
        super().on_train_with_cross_validation_start()
        print(self._trial_status(status="STARTED"))

    def on_train_with_cross_validation_end(self, was_pruned: bool) -> float:
        output = super().on_train_with_cross_validation_end(was_pruned)
        status = "PRUNED" if was_pruned else "COMPLETE"
        print(self._trial_status(status=status))
        return output

    def on_before_trainer_init(
        self,
        callbacks: list[L.Callback],
        fold_idx: int,
        train_group_ids: list[int],
        val_group_id: int,
    ):
        super().on_before_trainer_init(
            callbacks, fold_idx, train_group_ids, val_group_id
        )
        _early_pruning_callback = (
            OptunaPruningLightningCallback
            if LIGHTNING_VERSION_GTE_2
            else PyTorchLightningPruningCallback
        )
        # name is sort of a misnomer
        # this can always be registed to report results to optuna
        # but at the study level, the pruning can be toggled
        early_pruning_callback = _early_pruning_callback(
            trial=self._trial, monitor="val_loss"
        )
        self.add_callback_during_fold(callbacks, early_pruning_callback)

    def train_with_cross_validation(self) -> float:
        # lazy init to allow hparams to be sampled first
        # update name based on trial
        self.cv_trainer_kwargs["name"] = f"trial_{self._trial.number}"
        CrossValidationTrainer.__init__(self, **self.cv_trainer_kwargs)
        return super().train_with_cross_validation()


def _objective(
    trial: optuna.Trial, partial_trainer: partial[TuningCrossValidationTrainer]
) -> float:
    trainer = partial_trainer(trial=trial)
    return trainer.train_with_cross_validation()


def _parallel_objective(
    trial: optuna.Trial,
    queue: Queue[str],
    partial_trainer: partial[TuningCrossValidationTrainer],
):
    device_id = queue.get()
    trainer = partial_trainer(trial=trial, devices=device_id)
    output = trainer.train_with_cross_validation()
    queue.put(device_id)
    return output


def optimize(
    n_trials: int = 100,
    prune: bool = True,
    parallel: bool = False,
    configfile: Optional[Path] = None,
    **cv_trainer_kwargs,
):
    pruner: optuna.pruners.BasePruner = (
        optuna.pruners.MedianPruner() if prune else optuna.pruners.NopPruner()
    )
    strategy: str = cv_trainer_kwargs["strategy"]
    device_count: int = cv_trainer_kwargs["devices"]
    gpu_available_or_requested = (
        cv_trainer_kwargs["accelerator"] == "gpu" or torch.cuda.is_available()
    )

    # change default_root_dir to include the original expt name
    # then during the training for each trial, the expt name will be changed
    # to the trial number, and each trial trains for n CV folds called
    # version_0...version_n, thus the overall dir structure is:
    # rootdir/exptname/trial_X/version_X/...
    logdir = _get_logdir(
        cv_trainer_kwargs["default_root_dir"], cv_trainer_kwargs["name"]
    )
    cv_trainer_kwargs["default_root_dir"] = logdir

    if strategy in ("ddp", "ddp_spawn", "ddp_notebook") or (
        device_count > 1 and gpu_available_or_requested
    ):
        # in distributed training case, even with multi-gpu single node
        # cannot use the default InMemoryStorage. Instead, use a journaling fs
        # sqlite dbs do not work in a parallel tuning case, even multi-gpu single node
        # and other sql backends (mysql, postgresql) are still complicated with
        # multi-node parallelization <- require setting up server,
        # so a journaling fs is most appropriate for all cases, HOWEVER,
        # PyTorchLightningPruningCallback is only compatible with a RDBstorage backend
        # thus, we have to use sqlite backend, which means parallel gpu tuning may not
        # work
        fname = "tuning.db"
        fpath = logdir.joinpath(fname)

        # also the logdir must exist
        logdir.mkdir(parents=True, exist_ok=True)
        storage = f"sqlite:///{fpath}"
    else:
        # defaults back to InMemoryStorage
        storage = None

    study = optuna.create_study(
        storage=storage,
        direction="minimize",
        study_name=cv_trainer_kwargs["name"],
        pruner=pruner,
        load_if_exists=True,
    )

    # optuna callbacks are called after every time objective is finished
    callback = OptunaHyperparameterLogger(logdir=logdir)

    optimizer = partial(
        study.optimize,
        n_trials=n_trials,
        gc_after_trial=True,
        callbacks=[callback],
    )

    if gpu_available_or_requested and parallel:
        device_count: int = cv_trainer_kwargs.pop("devices")
        # TODO: what if no CUDA_VISIBLE_DEVICES?
        devices = [
            device.lstrip().rstrip()
            for device in os.environ["CUDA_VISIBLE_DEVICES"].split(",")
        ]
        with Manager() as manager:
            queue: Queue[str] = manager.Queue()  # type: ignore

            for n, device in enumerate(devices, start=1):
                if n > device_count:
                    break
                queue.put(device)

            partial_trainer = partial(
                TuningCrossValidationTrainer, configfile=configfile, **cv_trainer_kwargs
            )
            objective = partial(
                _parallel_objective,
                queue=queue,
                partial_trainer=partial_trainer,
            )

            with parallel_backend(backend="multiprocessing", n_jobs=device_count):
                optimizer(func=objective, n_jobs=device_count)
    else:
        partial_trainer = partial(
            TuningCrossValidationTrainer, configfile=configfile, **cv_trainer_kwargs
        )
        objective = partial(_objective, partial_trainer=partial_trainer)
        optimizer(func=objective)

    print(f"Number of finished trials: {len(study.trials)} out of {n_trials}")
    best_trial = study.best_trial
    print(
        f"Best trial: #{best_trial.number}\n"
        f"Average val loss of the final epoch across folds: {best_trial.value}"
    )