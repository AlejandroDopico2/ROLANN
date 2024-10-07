from models.ROLANN_incremental import ROLANN_Incremental
from models.Backbone import Backbone
from models.ROLANN import ROLANN
import torch.nn as nn
import torch
from typing import Optional


class RolanNET(nn.Module):
    def __init__(
        self,
        num_classes: int,
        activation: str = "logs",
        lamb: float = 0.01,
        pretrained: bool = True,
        backbone: Optional[Backbone] = None,
        in_channels: int = 3,
        sparse: bool = False,
        device: str = "cuda",
        dropout_rate: float = 0.0,
        freeze_mode: str = "all",
        incremental: bool = False,
        freeze_rolann: bool = False,
    ) -> None:
        super(RolanNET, self).__init__()

        self.device = device

        if backbone is not None:
            self.backbone = backbone(pretrained).to(self.device)
            self.backbone.set_input_channels(in_channels)
            self.freeze_backbone(freeze_mode)
        else:
            self.backbone = None

        if incremental:
            self.rolann = ROLANN_Incremental(
                num_classes,
                activation=activation,
                lamb=lamb,
                sparse=sparse,
                dropout_rate=dropout_rate,
                freeze_output=freeze_rolann,
            ).to(self.device)
        else:
            self.rolann = ROLANN(
                num_classes,
                activation=activation,
                lamb=lamb,
                sparse=sparse,
                dropout_rate=dropout_rate,
            ).to(self.device)

    def freeze_backbone(self, freeze_mode: str) -> None:
        if freeze_mode == "none":
            # No freezing
            for param in self.backbone.parameters():
                param.requires_grad = True
        elif freeze_mode == "all":
            # Freeze all layers
            for param in self.backbone.parameters():
                param.requires_grad = False
        elif freeze_mode == "partial":
            # Freeze all layers except the last few
            total_layers = len(list(self.backbone.children()))
            for i, child in enumerate(self.backbone.children()):
                if i < total_layers - 2:
                    for param in child.parameters():
                        param.requires_grad = False
                else:
                    for param in child.parameters():
                        param.requires_grad = True
        else:
            raise ValueError(
                f"Invalid freeze_mode: {freeze_mode}. Choose 'none', 'all', or 'partial'."
            )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.backbone:
            x = x.to(self.device)
            x = self.backbone(x).squeeze()

        x = x.to(self.device)
        x = self.rolann(x)

        return x

    @torch.no_grad
    def update_rolann(self, x: torch.Tensor, labels: torch.Tensor) -> None:
        if self.backbone:
            x = x.to(self.device)
            x = self.backbone(x).squeeze()

        x = x.to(self.device)

        self.rolann.aggregate_update(x, labels.to(self.device))
