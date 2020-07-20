import argparse
import logging
import os
import random
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pytorch_lightning as pl
import torch
from pytorch_lightning.utilities import rank_zero_info, rank_zero_only
from argparse import Namespace

from transformers import (
    AdamW,
    AutoConfig,
    AutoModel,
    AutoModelForPreTraining,
    AutoModelForQuestionAnswering,
    AutoModelForSeq2SeqLM,
    AutoModelForSequenceClassification,
    AutoModelForTokenClassification,
    AutoModelWithLMHead,
    AutoTokenizer,
    PretrainedConfig,
    PreTrainedTokenizer,
    get_linear_schedule_with_warmup,
)


logger = logging.getLogger(__name__)


MODEL_MODES = {
    "base": AutoModel,
    "sequence-classification": AutoModelForSequenceClassification,
    "question-answering": AutoModelForQuestionAnswering,
    "pretraining": AutoModelForPreTraining,
    "token-classification": AutoModelForTokenClassification,
    "language-modeling": AutoModelWithLMHead,
    "summarization": AutoModelForSeq2SeqLM,
    "translation": AutoModelForSeq2SeqLM,
}


def set_seed(args: argparse.Namespace):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.gpus > 0:
        torch.cuda.manual_seed_all(args.seed)


class BaseTransformer(pl.LightningModule):
    def __init__(
        self,
        hparams: argparse.Namespace,
        num_labels=None,
        mode="base",
        config=None,
        tokenizer=None,
        model=None,
        **config_kwargs
    ):
        """Initialize a model, tokenizer and config."""
        super().__init__()
        if type(hparams) == dict:
            hparams =  Namespace(**hparams)
        self.hparams = hparams  # TODO: move to self.save_hyperparameters()
        self.step_count = 0
        self.tfmr_ckpts = {}
        self.output_dir = Path(self.hparams.output_dir)
        cache_dir = self.hparams.cache_dir if self.hparams.cache_dir else None
        if config is None:
            self.config = AutoConfig.from_pretrained(
                self.hparams.config_name if self.hparams.config_name else self.hparams.model_name_or_path,
                **({"num_labels": num_labels} if num_labels is not None else {}),
                cache_dir=cache_dir,
                **config_kwargs,
            )
        else:
            self.config: PretrainedConfig = config
        if tokenizer is None:
            self.tokenizer = AutoTokenizer.from_pretrained(
                self.hparams.tokenizer_name if self.hparams.tokenizer_name else self.hparams.model_name_or_path,
                cache_dir=cache_dir,
            )
        else:
            self.tokenizer: PreTrainedTokenizer = tokenizer
        self.model_type = MODEL_MODES[mode]
        if model is None:
            self.model = self.model_type.from_pretrained(
                self.hparams.model_name_or_path,
                from_tf=bool(".ckpt" in self.hparams.model_name_or_path),
                config=self.config,
                cache_dir=cache_dir,
            )
        else:
            self.model = model

    def load_hf_checkpoint(self, *args, **kwargs):
        self.model = self.model_type.from_pretrained(*args, **kwargs)

    def configure_optimizers(self):
        "Prepare optimizer and schedule (linear warmup and decay)"
        model = self.model
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": self.hparams.weight_decay,
            },
            {
                "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
        ]
        optimizer = AdamW(optimizer_grouped_parameters, lr=self.hparams.learning_rate, eps=self.hparams.adam_epsilon)
        self.opt = optimizer
        return [optimizer]

    def optimizer_step(self, epoch, batch_idx, optimizer, optimizer_idx, second_order_closure=None, on_tpu=False, using_native_amp=False, using_lbfgs=False):
        optimizer.step()
        optimizer.zero_grad()
        self.lr_scheduler.step()  # By default, PL will only step every epoch.
        lrs = {f"lr_group_{i}": lr for i, lr in enumerate(self.lr_scheduler.get_lr())}
        self.logger.log_metrics(lrs)

    def test_step(self, batch, batch_nb):
        return self.validation_step(batch, batch_nb)

    def test_epoch_end(self, outputs):
        return self.validation_end(outputs)

    def train_dataloader(self):
        train_batch_size = self.hparams.train_batch_size
        dataloader = self.load_dataset("train", train_batch_size)

        t_total = (
            (len(dataloader.dataset) // (train_batch_size * max(1, self.hparams.gpus)))
            // self.hparams.gradient_accumulation_steps
            * float(self.hparams.num_train_epochs)
        )
        scheduler = get_linear_schedule_with_warmup(
            self.opt, num_warmup_steps=self.hparams.warmup_steps, num_training_steps=t_total
        )
        self.lr_scheduler = scheduler
        return dataloader

    def val_dataloader(self):
        return self.load_dataset("dev", self.hparams.eval_batch_size)

    def test_dataloader(self):
        return self.load_dataset("test", self.hparams.eval_batch_size)

    def _feature_file(self, mode):
        return os.path.join(
            self.hparams.data_dir,
            "cached_{}_{}_{}".format(
                mode,
                list(filter(None, self.hparams.model_name_or_path.split("/"))).pop(),
                str(self.hparams.max_seq_length),
            ),
        )

    @pl.utilities.rank_zero_only
    def on_save_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        save_path = self.output_dir.joinpath("best_tfmr")
        save_path.mkdir(exist_ok=True)
        self.model.config.save_step = self.step_count
        self.model.save_pretrained(save_path)
        self.tokenizer.save_pretrained(save_path)
        self.tfmr_ckpts[self.step_count] = save_path

class LoggingCallback(pl.Callback):
    @rank_zero_only
    def on_validation_end(self, trainer: pl.Trainer, pl_module: pl.LightningModule):
        rank_zero_info("***** Validation results *****")
        metrics = trainer.callback_metrics
        # Log results
        for key in sorted(metrics):
            if key not in ["log", "progress_bar"]:
                rank_zero_info("{} = {}\n".format(key, str(metrics[key])))

    @rank_zero_only
    def on_test_end(self, trainer: pl.Trainer, pl_module: pl.LightningModule):
        logger.info("***** Test results *****")
        metrics = trainer.callback_metrics
        # Log and save results to file
        output_test_results_file = os.path.join(pl_module.hparams.output_dir, "test_results.txt")
        with open(output_test_results_file, "w") as writer:
            for key in sorted(metrics):
                if key not in ["log", "progress_bar"]:
                    logger.info("{} = {}\n".format(key, str(metrics[key])))
                    writer.write("{} = {}\n".format(key, str(metrics[key])))


def generic_trainer(
    model: BaseTransformer,
    args: argparse.Namespace,
    early_stopping_callback=False,
    logger=True,  # can pass WandbLogger() here
    extra_callbacks=[],
    checkpoint_callback=None,
    logging_callback=None,
    **extra_train_kwargs
):
    # init model
    set_seed(args)
    odir = Path(model.hparams.output_dir)
    odir.mkdir(exist_ok=True)
    if checkpoint_callback is None:
        checkpoint_callback = pl.callbacks.ModelCheckpoint(
            filepath=args.output_dir, prefix="checkpoint", monitor="val_loss", mode="min", save_top_k=1
        )
    if logging_callback is None:
        logging_callback = LoggingCallback()

    train_params = {}

    if args.fp16:
        train_params["use_amp"] = args.fp16
        train_params["amp_level"] = args.fp16_opt_level
        train_params["num_tpu_cores"] = args.n_tpu_cores
        train_params["gpus"] = 0

    if args.gpus > 1:
        train_params["distributed_backend"] = "ddp"
        os.environ["TOKENIZERS_PARALLELISM"] = "true"
    trainer = pl.Trainer(
        logger=logger,
        accumulate_grad_batches=args.gradient_accumulation_steps,
        gpus=args.gpus,
        max_epochs=args.num_train_epochs,
        early_stop_callback=early_stopping_callback,
        gradient_clip_val=args.max_grad_norm,
        checkpoint_callback=checkpoint_callback,
        callbacks=[logging_callback] + extra_callbacks,
        fast_dev_run=args.fast_dev_run,
        val_check_interval=args.val_check_interval,
        weights_summary=None,
        resume_from_checkpoint=args.resume_from_checkpoint,
        **train_params,
    )

    return trainer