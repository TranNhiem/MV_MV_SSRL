# Inherence Design BYOL  solo-learn development team.
# Inherence Design BYOL  solo-learn development team.



import argparse
from typing import Any, Dict, List, Sequence, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from solo.losses.byol import byol_loss_func
#from solo.losses.massl import byol_loss_multi_views_func
from solo.methods.base import BaseMomentumMethod
from solo.utils.momentum import initialize_momentum_params


class MVAR(BaseMomentumMethod):
    def __init__(
        self,
        proj_output_dim: int,
        proj_hidden_dim: int,
        pred_hidden_dim: int,
        **kwargs,
    ):
        """
        Args:
            proj_output_dim (int): number of dimensions of projected features.
            proj_hidden_dim (int): number of neurons of the hidden layers of the projector.
            pred_hidden_dim (int): number of neurons of the hidden layers of the predictor.
        """

        super().__init__(**kwargs)

        # projector
        self.projector = nn.Sequential(
            nn.Linear(self.features_dim, proj_hidden_dim),
            nn.BatchNorm1d(proj_hidden_dim),
            nn.ReLU(),
            nn.Linear(proj_hidden_dim, proj_output_dim),
        )

        # momentum projector
        self.momentum_projector = nn.Sequential(
            nn.Linear(self.features_dim, proj_hidden_dim),
            nn.BatchNorm1d(proj_hidden_dim),
            nn.ReLU(),
            nn.Linear(proj_hidden_dim, proj_output_dim),
        )
        initialize_momentum_params(self.projector, self.momentum_projector)

        # predictor
        self.predictor = nn.Sequential(
            nn.Linear(proj_output_dim, pred_hidden_dim),
            nn.BatchNorm1d(pred_hidden_dim),
            nn.ReLU(),
            nn.Linear(pred_hidden_dim, proj_output_dim),
        )

    @staticmethod
    def add_model_specific_args(parent_parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
        parent_parser = super(MVAR, MVAR).add_model_specific_args(parent_parser)
        parser = parent_parser.add_argument_group("byol")

        # projector
        parser.add_argument("--proj_output_dim", type=int, default=256)
        parser.add_argument("--proj_hidden_dim", type=int, default=2048)

        # predictor
        parser.add_argument("--pred_hidden_dim", type=int, default=512)

        return parent_parser

    @property
    def learnable_params(self) -> List[dict]:
        """Adds projector and predictor parameters to the parent's learnable parameters.

        Returns:
            List[dict]: list of learnable parameters.
        """

        extra_learnable_params = [
            {"params": self.projector.parameters()},
            {"params": self.predictor.parameters()},
        ]
        return super().learnable_params + extra_learnable_params

    @property
    def momentum_pairs(self) -> List[Tuple[Any, Any]]:
        """Adds (projector, momentum_projector) to the parent's momentum pairs.

        Returns:
            List[Tuple[Any, Any]]: list of momentum pairs.
        """

        extra_momentum_pairs = [(self.projector, self.momentum_projector)]
        return super().momentum_pairs + extra_momentum_pairs

    def forward(self, X: torch.Tensor, *args, **kwargs) -> Dict[str, Any]:
        """Performs forward pass of the online backbone, projector and predictor.

        Args:
            X (torch.Tensor): batch of images in tensor format.

        Returns:
            Dict[str, Any]: a dict containing the outputs of the parent and the projected features.
        """
        
        ## Consider Editing Part for Extra 2 more Views
        out = super().forward(X, *args, **kwargs)
        print(out.shape)

        z = self.projector(out["feats"])
        p = self.predictor(z)
        return {**out, "z": z, "p": p}
        
    def _shared_step(
        self, feats: List[torch.Tensor], momentum_feats: List[torch.Tensor]
    ) -> torch.Tensor:

        Z = [self.projector(f) for f in feats]
        P = [self.predictor(z) for z in Z]
        print("length from Targe encod",len(Z))
        print("length of Online encod", len(P))
        # forward momentum backbone
        with torch.no_grad():
            Z_momentum = [self.momentum_projector(f) for f in momentum_feats]

        # ------- negative consine similarity loss -------
        
        neg_cos_sim_glob = 0
        ## If the Multi View the Loop Iteratively Corresponding
        print("length of Large Crops training",self.num_large_crops ) 
        for v1 in range(self.num_large_crops):
            # Views 2 remove the prior Views
            for v2 in np.delete(range(self.num_crops-self.num_small_crops), v1):
                neg_cos_sim_glob += byol_loss_func(P[v2], Z_momentum[v1], )
        
        # calculate std of features
        with torch.no_grad():
            z_std_glob = F.normalize(torch.stack(Z[: self.num_small_crops]), dim=-1).std(dim=1).mean()
           
        neg_cos_sim_loc= 0
        print("Length of small crop training", self.num_small_crops)
        for v1 in range(self.num_small_crops):
            # Views 2 remove the prior Views
            for v2 in np.delete(range(self.num_crops-self.num_large_crops), v1):
                neg_cos_sim_loc += byol_loss_func(P[(v2+self.num_large_crops)-1], Z_momentum[v1], )

        neg_cos_sim = (neg_cos_sim_glob + neg_cos_sim_loc)/2
        # calculate std of features
        with torch.no_grad():
            z_std_loc = F.normalize(torch.stack(Z[self.num_large_crops : ]), dim=-1).std(dim=1).mean()
        z_std=z_std_glob + z_std_loc
        
        return neg_cos_sim, z_std

    def training_step(self, batch: Sequence[Any], batch_idx: int) -> torch.Tensor:
        """Training step for BYOL reusing BaseMethod training step.

        Args:
            batch (Sequence[Any]): a batch of data in the format of [img_indexes, [X], Y], where
                [X] is a list of size num_crops containing batches of images.
            batch_idx (int): index of the batch.

        Returns:
            torch.Tensor: total loss composed of BYOL and classification loss.
        """

        out = super().training_step(batch, batch_idx)
        class_loss = out["loss"]

        neg_cos_sim, z_std = self._shared_step(out["feats"], out["momentum_feats"])

        metrics = {
            "train_neg_cos_sim": neg_cos_sim,
            "train_z_std": z_std,
        }
        self.log_dict(metrics, on_epoch=True, sync_dist=True)

        return neg_cos_sim + class_loss
