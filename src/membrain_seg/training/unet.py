from functools import partial
from typing import Dict, Tuple

import pytorch_lightning as pl
import torch
from monai.data import decollate_batch
from monai.metrics import DiceMetric
from monai.transforms import AsDiscrete, Compose, EnsureType, Lambda
from torch.optim.lr_scheduler import CosineAnnealingLR

# from monai.networks.nets import UNet as MonaiUnet
# The normal Monai DynUNet upsamples low-resolution layers to compare directly to GT
# My implementation leaves them in low resolution and compares to down-sampled GT
# Not sure which implementation is better
# To be discussed with Alister & Kevin
from ..training.optim_utils import (
    DeepSuperVisionLoss,
    DynUNetDirectDeepSupervision,  # I like to use deep supervision
    IgnoreLabelDiceCELoss,
    threshold_function,
)


class SemanticSegmentationUnet(pl.LightningModule):
    """Implementation of a Unet for semantic segmentation.

    This is uses the monai Unet. See the monai docs for more details
    on the parameters.
    https://docs.monai.io/en/stable/networks.html#unet

    Parameters
    ----------
    spatial_dims : int
        The number of spatial dimensions is in the data.
    in_channels : int
        The number of channels in the input tensor.
        The default value is 1.
    out_channels : int
        The number of channels in the output tensor.
        The default value is 1.
    channels : Tuple[int, ...]
        The number of channels in each layer of the encoder/decoder.
        Default value is (16, 32, 64, 128, 256)
    strides : Tuple[int, ...]
        The strides for the convolutions. Must have len(channels - 1) elements.
        Default value is (2, 2, 2, 2)
    num_res_units : int
        The number of residual subunits.
    learning_rate : float
        The learning rate for the Adam optimizer.
        Default value is 1e-4.
    image_key : str
        The value in the batch data dictionary containing the input image.
        Default value is "image".
    label_key : str
        The value in the batch data dictionary containing the labels.
        Default value is "label"
    roi_size : Tuple[int, ...]
        The size of the sliding window for the validation inference.
        Default value is (160, 160, 160).
    """

    def __init__(
        self,
        spatial_dims: int = 3,
        in_channels: int = 1,
        out_channels: int = 1,
        channels: Tuple[int, ...] = (16, 32, 64, 128, 256),
        # TODO: Do channel numbers need to be adjusted?
        strides: Tuple[int, ...] = (1, 2, 2, 2),
        num_res_units: int = 2,
        learning_rate: float = 1e-4,
        min_learning_rate: float = 1e-6,
        batch_size: int = 32,
        image_key: str = "image",
        label_key: str = "label",
        # path_dict: dict = {
        #     "train": ("../../../../data/imagesTr", "../../../../data/labelsTr"),
        #     "val": ("../../../../data/imagesVal", "../../../../data/labelsVal"),
        # },
        # TODO: Remove hard-coded stuff? Ideally won't need here.
        roi_size: Tuple[int, ...] = (160, 160, 160),
        max_epochs: int = 1000,
    ):
        super().__init__()

        # store parameters
        self.learning_rate = learning_rate
        self.min_learning_rate = min_learning_rate
        self.batch_size = batch_size
        self.image_key = image_key
        self.label_key = label_key
        # self.path_dict = path_dict
        self.roi_size = roi_size
        self.max_epochs = max_epochs

        # make the network
        self._model = DynUNetDirectDeepSupervision(
            spatial_dims=spatial_dims,
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=(3, 3, 3, 3),
            strides=strides,
            upsample_kernel_size=(1, 2, 2, 2),
            # filters=channels,
            # channels=channels,
            # num_res_units=num_res_units, #TODO: Residual units
            # apparently not supported for DynUnet
            # Reimplement? Or better switch to UNet and leave deep supervision?
            # Or adjust UNet to output deep layers?
            norm_name="INSTANCE",
            # norm=Norm.INSTANCE,  # I like the instance normalization better than
            # batchnorm in this case, as we will probably have
            # only small batch sizes, making BN more noisy
            deep_supervision=True,
            deep_supr_num=2,
        )
        ignore_dice_loss = IgnoreLabelDiceCELoss(ignore_label=2, reduction="mean")
        self.loss_function = DeepSuperVisionLoss(
            ignore_dice_loss, weights=[1.0, 0.5, 0.25]
        )

        # validation metric
        self.dice_metric = DiceMetric(
            include_background=False, reduction="mean", get_not_nans=False
        )

        # transforms for val metric calculation
        threshold_function_partial = partial(threshold_function, threshold_value=0.0)
        self.post_pred = Compose(
            [
                EnsureType("tensor", device="cpu"),
                Lambda(threshold_function_partial),
                AsDiscrete(
                    to_onehot=2
                ),  # No argmax needed, since only 1 channel output
            ]
        )
        self.post_label = Compose(
            [
                EnsureType("tensor", device="cpu"),
                AsDiscrete(to_onehot=2),
            ]
        )

        self.training_step_outputs = []
        self.validation_step_outputs = []

    def forward(self, x) -> torch.Tensor:
        """Implementation of the forward pass.

        See the pytorch-lightning module documentation for details.
        """
        return self._model(x)

    def configure_optimizers(self):
        """Set up the Adam optimzier.

        See the pytorch-lightning module documentation for details.
        """
        optimizer = torch.optim.Adam(self._model.parameters(), self.learning_rate)
        scheduler = CosineAnnealingLR(
            optimizer, T_max=self.max_epochs, eta_min=self.min_learning_rate
        )
        return [optimizer], [scheduler]

    # TODO Does it also work with dataloaders commented? They should already be in the
    # datamodule
    # def train_dataloader(self):
    #     self.train_dataset = CryoETMemSegDataset(self.path_dict['train'][0],
    #         self.path_dict['train'][1], train=True)
    #     return DataLoader(self.train_dataset, batch_size=self.batch_size,
    # shuffle=True, num_workers=4)

    # def val_dataloader(self):
    #     self.val_dataset = CryoETMemSegDataset(self.path_dict['val'][0],
    #         self.path_dict['val'][1], train=False)
    #     return DataLoader(self.val_dataset, batch_size=self.batch_size,
    # shuffle=False, num_workers=4)

    # def test_dataloader(self):
    #     if 'test' in self.path_dict.keys():
    #         self.test_dataset = CryoETMemSegDataset(self.path_dict['test'][0],
    #             self.path_dict['test'][1], train=False)
    #         return DataLoader(self.test_dataset, batch_size=self.batch_size,
    # shuffle=False, num_workers=4)
    #     else:
    #         return None

    def training_step(
        self, batch: Dict[str, torch.Tensor], batch_idx: int
    ) -> Dict[str, float]:
        """Implementation of one training step.

        See the pytorch-lightning module documentation for details.
        """
        images, labels = batch["image"], batch["label"]
        output = self.forward(images)
        loss = self.loss_function(output, labels)

        # self.log("training_loss", loss, batch_size=len(images))
        # Should we log every training step or only epoch-wise?

        stats_dict = {"train_loss": loss, "train_number": output[0].shape[0]}
        self.training_step_outputs.append(stats_dict)
        return {"loss": loss}

    def on_train_epoch_end(self):
        """What happens after each training epoch?.

        Learning rate scheduler makes one step.
        Then training loss is logged.
        """
        # Perform 1 LR scheduler step
        sch = self.lr_schedulers()
        sch.step()

        outputs = self.training_step_outputs
        train_loss, num_items = 0, 0
        for output in outputs:
            train_loss += output["train_loss"].sum().item()
            num_items += output["train_number"]
        mean_train_loss = torch.tensor(train_loss / num_items)

        self.log("train_loss", mean_train_loss, batch_size=num_items)

        self.validation_step_outputs = []
        print("EPOCH Training loss", mean_train_loss)
        return {"train_loss": mean_train_loss}

    def validation_step(self, batch, batch_idx):
        """Implementation of one validation step.

        This performs inference on the entire validation image
        using a sliding window. See the pytorch-lightning
        module documentation for details.
        """
        images, labels = batch[self.image_key], batch[self.label_key]
        # sw_batch_size = 4
        # outputs = sliding_window_inference(
        #     images, self.roi_size, sw_batch_size, self.forward
        # )
        outputs = self.forward(images)

        loss = self.loss_function(outputs, labels)

        # Cloning and adjusting preds & labels for Dice.
        # Could also use the same labels, but maybe we want to
        # compute more stats?
        outputs4dice = outputs[0].clone()
        labels4dice = labels[0].clone()
        outputs4dice[labels4dice == 2] = 0
        labels4dice[labels4dice == 2] = 0  # Need to set to zero before post_label
        # Otherwise we have 3 classes
        outputs4dice = [self.post_pred(i) for i in decollate_batch(outputs4dice)]
        labels4dice = [self.post_label(i) for i in decollate_batch(labels4dice)]
        self.dice_metric(y_pred=outputs4dice, y=labels4dice)

        stats_dict = {"val_loss": loss, "val_number": outputs[0].shape[0]}
        self.validation_step_outputs.append(stats_dict)
        return stats_dict

    def on_validation_epoch_end(self):
        """Calculate validation loss/metric summary."""
        outputs = self.validation_step_outputs
        val_loss, num_items = 0, 0
        for output in outputs:
            val_loss += output["val_loss"].sum().item()
            num_items += output["val_number"]
        mean_val_dice = self.dice_metric.aggregate().item()
        self.dice_metric.reset()
        mean_val_loss = torch.tensor(val_loss / num_items)

        self.log("val_loss", mean_val_loss, batch_size=num_items)
        self.log("val_metric", mean_val_dice, batch_size=num_items)

        self.validation_step_outputs = []
        print("EPOCH Validation loss", mean_val_loss)
        print("EPOCH Validation dice", mean_val_dice)
        return {"val_loss": mean_val_loss, "val_metric": mean_val_dice}