import os
from pathlib import Path
from optimization.constants import ASSETS_DIR_NAME
from utils.metrics_accumulator import MetricsAccumulator
import time
from numpy import random
from optimization.augmentations import ImageAugmentations as ImageAugmentations
from PIL import Image
import torch
from torchvision import transforms
import torchvision.transforms.functional as F
from torchvision.transforms import functional as TF
from torch.nn.functional import mse_loss
from optimization.losses import range_loss, d_clip_loss, d_clip_dir_loss, mse_loss, get_features, zecon_loss_direct
import numpy as np

from CLIP import clip
from guided_diffusion.guided_diffusion.script_util import (
    create_model_and_diffusion,
    model_and_diffusion_defaults,
)
from torchvision import models
from utils.visualization import show_edited_masked_image
import matplotlib.pyplot as plt

# import ipdb


class ImageEditor:
    def __init__(self, args) -> None:
        self.saved_image = {}
        self.saved_image["text"] = []
        self.saved_image["image"] = []
        self.saved_image["image+text"] = []
        self.saved_image["hybrid"] = []

        self.args = args
        os.makedirs(self.args.output_path, exist_ok=True)

        if self.args.export_assets:
            self.assets_path = Path(os.path.join(
                self.args.output_path, ASSETS_DIR_NAME))
            os.makedirs(self.assets_path, exist_ok=True)
        if self.args.seed is not None:
            torch.manual_seed(self.args.seed)
            np.random.seed(self.args.seed)
            random.seed(self.args.seed)

        self.model_config = model_and_diffusion_defaults(self.args)

        # Load models
        self.device = torch.device(
            f"cuda:{self.args.gpu_id}" if torch.cuda.is_available() else "cpu"
        )
        print("Using device:", self.device)
        if self.args.data == 'imagenet':
            self.model, self.diffusion = create_model_and_diffusion(
                **self.model_config)
            self.model.load_state_dict(
                torch.load(
                    "./ckpt/256x256_diffusion_uncond.pt",
                    map_location="cpu",
                )
            )
        elif self.args.data == 'ffhq':
            self.model_config.update(
                {
                    "num_channels": 128,
                    "num_head_channels": 64,
                    "num_res_blocks": 1,
                    "attention_resolutions": "16",
                    "resblock_updown": True,
                    "use_fp16": False,
                }
            )
            self.model, self.diffusion = create_model_and_diffusion(
                **self.model_config)
            self.model.load_state_dict(
                torch.load(
                    # "./ckpt/ffhq_10m.pt",
                    "./ckpt/ffhq_baseline.pt",
                    map_location="cpu",
                )
            )

        self.model.requires_grad_(False).eval().to(self.device)
        for name, param in self.model.named_parameters():
            if "qkv" in name or "norm" in name or "proj" in name:
                param.requires_grad_()

        if self.model_config["use_fp16"]:
            self.model.convert_to_fp16()

        self.clip_model = (
            clip.load("ViT-B/16", device=self.device,
                      jit=False)[0].eval().requires_grad_(False)
        )

        self.clip_size = self.clip_model.visual.input_resolution
        self.clip_normalize = transforms.Normalize(
            mean=[0.48145466, 0.4578275, 0.40821073], std=[0.26862954, 0.26130258, 0.27577711]
        )

        self.image_augmentations = ImageAugmentations(
            224, self.args.aug_prob, self.args.patch_min, self.args.patch_max, patch=False)
        self.patch_augmentations = ImageAugmentations(
            224, self.args.aug_prob, self.args.patch_min, self.args.patch_max, patch=True)

        self.metrics_accumulator = MetricsAccumulator()

        if self.args.l_vgg > 0:
            self.vgg = models.vgg19(pretrained=True).features
            self.vgg.to(self.device)
            self.vgg.eval().requires_grad_(False)

        self.vgg_normalize = transforms.Normalize(
            mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
        )
        self.image_size = (
            self.model_config["image_size"], self.model_config["image_size"])
        self.style_image_pil = Image.open(self.args.ref_image).convert("RGB")
        style_image = self.style_image_pil.resize(
            self.image_size, Image.LANCZOS)  # type: ignore
        style_image = (
            TF.to_tensor(style_image).to(
                self.device).unsqueeze(0).mul(2).sub(1)
        )
        self.style_image = style_image

        # name rule : method_loss name
        self.matrix = {}
        self.matrix["clip_prompt"] = []
        self.matrix["clip_image"] = []
        self.matrix["clip_gram"] = []
        self.matrix["gram_prompt"] = []
        self.matrix["gram_image"] = []
        self.matrix["gram_gram"] = []
        self.matrix["hybrid_prompt"] = []
        self.matrix["hybrid_image"] = []
        self.matrix["hybrid_gram"] = []

    def unscale_timestep(self, t):
        unscaled_timestep = (t * (self.diffusion.num_timesteps / 1000)).long()

        return unscaled_timestep

    def clip_global_loss(self, x_in, text_embed):
        clip_loss = torch.tensor(0)
        augmented_input = self.image_augmentations(
            x_in, num_patch=self.args.n_patch).add(1).div(2)
        clip_in = self.clip_normalize(augmented_input)
        image_embeds = self.clip_model.encode_image(clip_in).float()
        dists = d_clip_loss(image_embeds, text_embed)
        for i in range(self.args.batch_size):
            clip_loss = clip_loss + dists[i:: self.args.batch_size].mean()

        return clip_loss

    def clip_global_patch_loss(self, x_in, text_embed):
        clip_loss = torch.tensor(0)
        augmented_input = self.patch_augmentations(
            x_in, num_patch=self.args.n_patch).add(1).div(2)
        clip_in = self.clip_normalize(augmented_input)
        image_embeds = self.clip_model.encode_image(clip_in).float()
        dists = d_clip_loss(image_embeds, text_embed)
        for i in range(self.args.batch_size):
            clip_loss = clip_loss + dists[i:: self.args.batch_size].mean()

        return clip_loss

    def clip_global_loss_feature(self, x_in, y_in):
        clip_loss = torch.tensor(0)
        augmented_input = self.image_augmentations(
            x_in, num_patch=self.args.n_patch).add(1).div(2)
        clip_in = self.clip_normalize(augmented_input)
        x_image_embeds = self.clip_model.encode_image(clip_in).float()

        augmented_input = self.image_augmentations(
            y_in, num_patch=self.args.n_patch).add(1).div(2)
        clip_in = self.clip_normalize(augmented_input)
        y_image_embeds = self.clip_model.encode_image(clip_in).float()
        dists = d_clip_loss(x_image_embeds, y_image_embeds)
        for i in range(self.args.batch_size):
            clip_loss = clip_loss + dists[i:: self.args.batch_size].mean()

        return clip_loss

    def clip_global_patch_loss_feature(self, x_in, y_in):
        clip_loss = torch.tensor(0)
        augmented_input = self.patch_augmentations(
            x_in, num_patch=self.args.n_patch).add(1).div(2)
        clip_in = self.clip_normalize(augmented_input)
        x_image_embeds = self.clip_model.encode_image(clip_in).float()

        augmented_input = self.patch_augmentations(
            y_in, num_patch=self.args.n_patch).add(1).div(2)
        clip_in = self.clip_normalize(augmented_input)
        y_image_embeds = self.clip_model.encode_image(clip_in).float()
        dists = d_clip_loss(x_image_embeds, y_image_embeds)
        for i in range(self.args.batch_size):
            clip_loss = clip_loss + dists[i:: self.args.batch_size].mean()

        return clip_loss

    def clip_dir_loss(self, x_in, y_in, text_embed, text_y_embed):
        clip_loss = torch.tensor(0)

        augmented_input_x = self.image_augmentations(
            x_in, num_patch=self.args.n_patch).add(1).div(2)
        augmented_input_y = self.image_augmentations(
            y_in, num_patch=self.args.n_patch).add(1).div(2)

        clip_in_x = self.clip_normalize(augmented_input_x)
        clip_in_y = self.clip_normalize(augmented_input_y)

        image_embeds_x = self.clip_model.encode_image(clip_in_x).float()
        image_embeds_y = self.clip_model.encode_image(clip_in_y).float()
        dists = d_clip_dir_loss(
            image_embeds_x, image_embeds_y, text_embed, text_y_embed)
        for i in range(self.args.batch_size):
            clip_loss = clip_loss + dists[i:: self.args.batch_size].mean()

        return clip_loss

    def clip_dir_patch_loss(self, x_in, y_in, text_embed, text_y_embed):
        clip_loss = torch.tensor(0)
        augmented_input_x = self.patch_augmentations(
            x_in, num_patch=self.args.n_patch).add(1).div(2)
        augmented_input_y = self.patch_augmentations(
            y_in, num_patch=self.args.n_patch, is_global=True).add(1).div(2)

        clip_in_x = self.clip_normalize(augmented_input_x)
        clip_in_y = self.clip_normalize(augmented_input_y)
        image_embeds_x = self.clip_model.encode_image(clip_in_x).float()
        image_embeds_y = self.clip_model.encode_image(clip_in_y).float()
        dists = d_clip_dir_loss(
            image_embeds_x, image_embeds_y, text_embed, text_y_embed)
        for i in range(self.args.batch_size):
            clip_loss = clip_loss + dists[i:: self.args.batch_size].mean()

        return clip_loss

    def clip_dir_patch_loss_feature(self, x_in, y_in, z_in, text_y_embed):
        clip_loss = torch.tensor(0)
        augmented_input_x = self.patch_augmentations(
            x_in, num_patch=self.args.n_patch).add(1).div(2)
        augmented_input_y = self.patch_augmentations(
            y_in, num_patch=self.args.n_patch, is_global=True).add(1).div(2)
        augmented_input_z = self.patch_augmentations(
            z_in, num_patch=self.args.n_patch, is_global=True).add(1).div(2)

        clip_in_x = self.clip_normalize(augmented_input_x)
        clip_in_y = self.clip_normalize(augmented_input_y)
        clip_in_z = self.clip_normalize(augmented_input_z)

        image_embeds_x = self.clip_model.encode_image(clip_in_x).float()
        image_embeds_y = self.clip_model.encode_image(clip_in_y).float()
        image_embeds_z = self.clip_model.encode_image(clip_in_z).float()

        dists = d_clip_dir_loss(
            image_embeds_x, image_embeds_y, image_embeds_z, text_y_embed)
        for i in range(self.args.batch_size):
            clip_loss = clip_loss + dists[i:: self.args.batch_size].mean()

        return clip_loss

    def zecon_loss(self, x_in, y_in, t):
        loss = zecon_loss_direct(
            self.model, x_in, y_in, torch.zeros_like(t, device=self.device))
        return loss.mean()

    def mse_loss(self, x_in, y_in):
        loss = mse_loss(x_in, y_in)
        return loss.mean()

    def vgg_loss(self, x_in, y_in):
        content_features = get_features(self.vgg_normalize(x_in), self.vgg)
        target_features = get_features(self.vgg_normalize(y_in), self.vgg)
        loss = 0

        loss += torch.mean((target_features['conv1_1'] -
                           content_features['conv1_1']) ** 2)
        loss += torch.mean((target_features['conv2_1'] -
                           content_features['conv2_1']) ** 2)
        return loss.mean()

    def gram_matrix(self, features):
        batch_size, num_channels, height, width = features.size()
        features = features.view(batch_size * num_channels, height * width)
        gram = torch.mm(features, features.t())
        return gram.div(batch_size * num_channels * height * width)

    def vgg_loss_feature_gram(self, x_in, y_in):
        content_features = get_features(self.vgg_normalize(x_in), self.vgg)
        target_features = get_features(self.vgg_normalize(y_in), self.vgg)
        loss = 0.0
        layers = {'0': 'conv1_1',
                  '2': 'conv1_2',
                  '5': 'conv2_1',
                  '7': 'conv2_2',
                  '10': 'conv3_1',
                  '19': 'conv4_1',
                  '21': 'conv4_2',
                  '28': 'conv5_1',
                  '31': 'conv5_2'
                  }
        for key in layers:
            target_gram = self.gram_matrix(target_features[layers[key]])
            content_gram = self.gram_matrix(content_features[layers[key]])
            loss += torch.mean((target_gram - content_gram) ** 2)
        return loss

    def vgg_loss_feature(self, x_in, y_in):
        content_features = get_features(self.vgg_normalize(x_in), self.vgg)
        target_features = get_features(self.vgg_normalize(y_in), self.vgg)
        loss = 0
        '''
        layers = {'0': 'conv1_1',
                  '2': 'conv1_2',
                  '5': 'conv2_1',
                  '7': 'conv2_2',
                  '10': 'conv3_1',
                  '19': 'conv4_1',
                  '21': 'conv4_2',
                  '28': 'conv5_1',
                  '31': 'conv5_2'
                 }
        '''
        loss += torch.mean((target_features['conv1_1'] -
                           content_features['conv1_1']) ** 2)
        loss += torch.mean((target_features['conv1_2'] -
                           content_features['conv1_2']) ** 2)
        loss += torch.mean((target_features['conv2_1'] -
                           content_features['conv2_1']) ** 2)
        loss += torch.mean((target_features['conv2_2'] -
                           content_features['conv2_2']) ** 2)
        loss += torch.mean((target_features['conv3_1'] -
                           content_features['conv3_1']) ** 2)
        loss += torch.mean((target_features['conv4_1'] -
                           content_features['conv4_1']) ** 2)
        loss += torch.mean((target_features['conv4_2'] -
                           content_features['conv4_2']) ** 2)
        loss += torch.mean((target_features['conv5_1'] -
                           content_features['conv5_1']) ** 2)
        loss += torch.mean((target_features['conv5_2'] -
                           content_features['conv5_2']) ** 2)

        return loss.mean()

    def get_clip_score_text(self, x_in, y_in):
        with torch.no_grad():
            image1_embedding = self.clip_model.encode_image(x_in)
        similarity_score = clip.cosine_similarity(
            image1_embedding, y_in).item()
        return similarity_score

    def get_clip_score_image(self, x_in, y_in):
        with torch.no_grad():
            image1_embedding = self.clip_model.encode_image(x_in)
            image2_embedding = self.clip_model.encode_image(y_in)
        similarity_score = clip.cosine_similarity(
            image1_embedding, image2_embedding).item()
        return similarity_score

    def save_image(self):
        output_len = len(str(len(self.saved_image["hybrid"])))
        for i in range(len(self.saved_image["hybrid"])):
            visualization_path = visualization_path = Path(
                os.path.join(self.args.output_path, self.args.output_file)
            )
            fig, axs = plt.subplots(2, 2, figsize=(10, 8))

            axs[0, 0].imshow(self.style_image_pil)
            axs[0, 0].set_title("prompt : " + self.args.prompt_tgt)

            try:
                image = self.saved_image["text"][i]
                image = (TF.to_tensor(image).to(
                    self.device).unsqueeze(0).mul(2).sub(1))
                text = self.clip_model.encode_text(
                    clip.tokenize(self.args.prompt_tgt).to(self.device)
                ).float()
                a = self.clip_global_loss(image, text)
                b = self.clip_global_loss_feature(image, self.style_image)
                c = self.vgg_loss_feature_gram(image, self.style_image)

                axs[0, 1].imshow(self.saved_image["text"][i])
                axs[0, 1].set_title('clip + gram')
                axs[0, 1].set_xlabel(
                    'CLIP SCORE(with prompt) = {}\nCLIP SCORE(with image) = {}\nGRAM SCORE(with image) = {}'.format(a, b, c))

                if i == output_len-1:
                    self.matrix["hybrid_prompt"].append(float(a))
                    self.matrix["hybrid_image"].append(float(b))
                    self.matrix["hybrid_gram"].append(float(c))
            except:
                pass

            try:
                image = self.saved_image["image"][i]
                image = (
                    TF.to_tensor(image).to(
                        self.device).unsqueeze(0).mul(2).sub(1)
                )
                a = self.clip_global_loss(image, text)
                b = self.clip_global_loss_feature(image, self.style_image)
                c = self.vgg_loss_feature_gram(image, self.style_image)
                axs[1, 0].imshow(self.saved_image["image"][i])
                axs[1, 0].set_title('clip score')
                axs[1, 0].set_xlabel(
                    'CLIP SCORE(with prompt) = {}\nCLIP SCORE(with image) = {}\nGRAM SCORE(with image) = {}'.format(a, b, c))

                if i == output_len-1:
                    self.matrix["clip_prompt"].append(float(a))
                    self.matrix["clip_image"].append(float(b))
                    self.matrix["clip_gram"].append(float(c))
            except:
                pass

            try:
                image = self.saved_image["image+text"][i]
                image = (TF.to_tensor(image).to(
                    self.device).unsqueeze(0).mul(2).sub(1))

                a = self.clip_global_loss(image, text)
                b = self.clip_global_loss_feature(image, self.style_image)
                c = self.vgg_loss_feature_gram(image, self.style_image)
                axs[1, 1].imshow(self.saved_image["image+text"][i])
                axs[1, 1].set_title('vgg_gram matrix mse')
                axs[1, 1].set_xlabel(
                    'CLIP SCORE(with prompt) = {}\nCLIP SCORE(with image) = {}\nGRAM SCORE(with image) = {}'.format(a, b, c))

                if i == output_len-1:
                    self.matrix["gram_prompt"].append(float(a))
                    self.matrix["gram_image"].append(float(b))
                    self.matrix["gram_gram"].append(float(c))
            except:
                pass

            # 調整子圖間距
            plt.tight_layout()

            filename = Path(self.args.init_image).stem
            # visualization_path = visualization_path.with_name(
            #     "{}_{}_{}_{}{}".format(filename, self.args.prompt_tgt, "{:0{width}d}".format(
            #         i, width=output_len),self.args.l_gram, visualization_path.suffix)
            # )
            visualization_path = visualization_path.with_name(
                "{}".format(visualization_path.suffix)
            )
            # plt.savefig(visualization_path)
            
            # visualization_path_hybrid = str(
            #     visualization_path).replace('.png', '_hybrid_{}.png'.format(Path(self.args.ref_image).stem))
            visualization_path_hybrid = str(
                visualization_path).replace('.png', '{}-{}.png'.format(Path(self.args.init_image).stem,Path(self.args.ref_image).stem))


            visualization_path_image = str(
                visualization_path).replace('.png', '_clip_{}.png'.format(self.args.ref_image))

            visualization_path_image_text = str(
                visualization_path).replace('.png', '_gram_{}.png'.format(self.args.ref_image))

            plt.imsave(visualization_path_hybrid, self.saved_image["hybrid"][i])
    
    def edit_image_by_hybrid(self):

        text_embed = self.clip_model.encode_text(
            clip.tokenize(self.args.prompt_tgt).to(self.device)
        ).float()
        text_y_embed = self.clip_model.encode_text(
            clip.tokenize(self.args.prompt_src).to(self.device)
        ).float()

        self.image_size = (
            self.model_config["image_size"], self.model_config["image_size"])
        self.init_image_pil = Image.open(self.args.init_image).convert("RGB")
        self.init_image_pil = self.init_image_pil.resize(
            self.image_size, Image.LANCZOS)  # type: ignore
        self.init_image = (
            TF.to_tensor(self.init_image_pil).to(
                self.device).unsqueeze(0).mul(2).sub(1)
        )
        visualization_path = visualization_path = Path(
            os.path.join(self.args.output_path, self.args.output_file)
        )

        def cond_fn(x, t, y=None):
            if self.args.prompt_tgt == "":
                return torch.zeros_like(x)

            with torch.enable_grad():
                x = x.detach().requires_grad_()
                t = self.unscale_timestep(t)

                out = self.diffusion.p_mean_variance(
                    self.model, x, t, clip_denoised=False, model_kwargs={"y": y}
                )

                fac = self.diffusion.sqrt_one_minus_alphas_cumprod[t[0].item()]
                x_in = out["pred_xstart"] * fac + x * (1 - fac)

                loss = torch.tensor(0)

                if self.args.l_clip_global_patch != 0:
                    vgg_loss = self.vgg_loss_feature_gram(
                        x_in, self.style_image) * self.args.l_gram
                    loss = loss + vgg_loss
                    self.metrics_accumulator.update_metric(
                        "vgg_loss_feature_gram : ", vgg_loss.item())

                if self.args.l_clip_global_patch != 0:
                    clip_patch_loss = (self.clip_global_patch_loss_feature(
                        x_in, self.style_image) * self.args.l_clip_global_patch)
                    loss = loss + clip_patch_loss
                    self.metrics_accumulator.update_metric(
                        "clip_patch_loss", clip_patch_loss.item())

                if self.args.l_clip_dir != 0:
                    y_t = self.diffusion.q_sample(self.init_image, t)
                    y_in = self.init_image * fac + y_t * (1 - fac)

                    clip_dir_loss = (self.clip_dir_patch_loss_feature(
                        x_in, y_in, self.style_image, text_y_embed) * self.args.l_clip_dir_patch)
                    loss = loss + clip_dir_loss
                    self.metrics_accumulator.update_metric(
                        "clip_dir_loss", clip_dir_loss.item())

                if self.args.l_zecon != 0:
                    y_t = self.diffusion.q_sample(self.init_image, t)
                    y_in = self.init_image * fac + y_t * (1 - fac)

                    zecon_loss = self.zecon_loss(
                        x_in, y_in, t) * self.args.l_zecon
                    loss = loss + zecon_loss
                    self.metrics_accumulator.update_metric(
                        "zecon_loss", zecon_loss.item())

                if self.args.l_mse != 0 and t.item() < 700:
                    y_t = self.diffusion.q_sample(self.init_image, t)
                    y_in = self.init_image * fac + y_t * (1 - fac)

                    mse_loss = self.mse_loss(x_in, y_in) * self.args.l_mse
                    loss = loss + mse_loss
                    self.metrics_accumulator.update_metric(
                        "mse_loss", mse_loss.item())

                if self.args.l_vgg != 0 and t.item() < 800:
                    y_t = self.diffusion.q_sample(self.init_image, t)
                    y_in = self.init_image * fac + y_t * (1 - fac)

                    vgg_loss = self.vgg_loss(x_in, y_in) * self.args.l_vgg
                    loss = loss + vgg_loss
                    self.metrics_accumulator.update_metric(
                        "vgg_loss", vgg_loss.item())

                if self.args.range_lambda != 0:
                    r_loss = range_loss(
                        out["pred_xstart"]).sum() * self.args.range_lambda
                    loss = loss + r_loss
                    self.metrics_accumulator.update_metric(
                        "range_loss", r_loss.item())
                self.metrics_accumulator.update_metric(
                    "total_loss", loss.item())
                return -torch.autograd.grad(loss, x)[0]

        save_image_interval = self.diffusion.num_timesteps // 5
        for iteration_number in range(self.args.iterations_num):
            fw = self.args.diffusion_type.split('_')[0]
            bk = self.args.diffusion_type.split('_')[-1]

            # Forward DDIM
            if fw == 'ddim':
                print("Forward Process to noise")
                noise = self.diffusion.ddim_reverse_sample_loop(
                    self.model,
                    self.init_image,
                    clip_denoised=False,
                    skip_timesteps=self.args.skip_timesteps,
                )

            # Forward DDPM
            elif fw == 'ddpm':
                init_image_batch = torch.tile(
                    self.init_image, dims=(self.args.batch_size, 1, 1, 1))
                noise = self.diffusion.q_sample(
                    x_start=init_image_batch,
                    t=torch.tensor(self.diffusion.num_timesteps-int(
                        self.args.skip_timesteps), dtype=torch.long, device=self.device),
                    noise=torch.randn(
                        (self.args.batch_size, 3, self.model_config["image_size"], self.model_config["image_size"]), device=self.device),
                )
            else:
                raise ValueError

            # Reverse DDPM
            if bk == 'ddpm':
                samples = self.diffusion.p_sample_loop_progressive(
                    self.model,
                    (
                        self.args.batch_size,
                        3,
                        self.model_config["image_size"],
                        self.model_config["image_size"],
                    ),
                    noise=noise if fw == 'ddim' else None,
                    clip_denoised=False,
                    model_kwargs={},
                    cond_fn=cond_fn,
                    progress=True,
                    skip_timesteps=self.args.skip_timesteps,
                    init_image=self.init_image,
                )

            # Reverse DDIM
            elif bk == 'ddim':
                samples = self.diffusion.ddim_sample_loop_progressive(
                    self.model,
                    (
                        self.args.batch_size,
                        3,
                        self.model_config["image_size"],
                        self.model_config["image_size"],
                    ),
                    noise=noise,
                    clip_denoised=False,
                    model_kwargs={},
                    cond_fn=cond_fn,
                    progress=True,
                    skip_timesteps=self.args.skip_timesteps,
                    eta=self.args.eta,
                )

            else:
                raise ValueError

            intermediate_samples = [[] for i in range(self.args.batch_size)]
            total_steps = self.diffusion.num_timesteps - self.args.skip_timesteps - 1
            for j, sample in enumerate(samples):
                should_save_image = j % save_image_interval == 0 or j == total_steps
                if should_save_image or self.args.save_video:
                    self.metrics_accumulator.print_average_metric()

                    for b in range(self.args.batch_size):
                        pred_image = sample["pred_xstart"][b]
                        pred_image = pred_image.add(1).div(2).clamp(0, 1)
                        pred_image_pil = TF.to_pil_image(pred_image)

                        filename = Path(self.args.init_image).stem
                        visualization_path = visualization_path.with_name(
                            f"{filename}_{self.args.prompt_tgt}_{iteration_number}{visualization_path.suffix}"
                        )

                        if self.args.export_assets:
                            pred_path = self.assets_path / visualization_path.name
                            pred_image_pil.save(pred_path)

                        intermediate_samples[b].append(pred_image_pil)
                        if should_save_image:
                            '''
                            show_edited_masked_image(
                                title=self.args.prompt_tgt,
                                source_image=self.init_image_pil,
                                edited_image=pred_image_pil,
                                path=visualization_path,
                                # distance=f"{self.get_clip_score_text(self.init_image,text_embed):.3f}"
                            )
                            '''

                            visualization_path2 = str(
                                visualization_path).replace('.png', '_output_promet.png')
                            pred_image_arr = np.array(pred_image_pil)
        self.saved_image["hybrid"].append(pred_image_arr)
        # plt.imsave(visualization_path2, pred_image_arr)