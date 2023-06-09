"""This module manages the InvokeAI `models.yaml` file, mapping
symbolic diffusers model names to the paths and repo_ids used
by the underlying `from_pretrained()` call.

For fetching models, use manager.get_model('symbolic name'). This will
return a SDModelInfo object that contains the following attributes:
   
   * context -- a context manager Generator that loads and locks the 
                model into GPU VRAM and returns the model for use. 
                See below for usage.
   * name -- symbolic name of the model
   * type -- SDModelType of the model
   * hash -- unique hash for the model
   * location -- path or repo_id of the model
   * revision -- revision of the model if coming from a repo id,
                e.g. 'fp16'
   * precision -- torch precision of the model

Typical usage:

   from invokeai.backend import ModelManager

   manager = ModelManager(
                 config='./configs/models.yaml',
                 max_cache_size=8
             ) # gigabytes

   model_info = manager.get_model('stable-diffusion-1.5', SDModelType.Diffusers)
   with model_info.context as my_model:
      my_model.latents_from_embeddings(...)

The manager uses the underlying ModelCache class to keep
frequently-used models in RAM and move them into GPU as needed for
generation operations. The optional `max_cache_size` argument
indicates the maximum size the cache can grow to, in gigabytes. The
underlying ModelCache object can be accessed using the manager's "cache"
attribute.

Because the model manager can return multiple different types of
models, you may wish to add additional type checking on the class
of model returned. To do this, provide the option `model_type`
parameter:

    model_info = manager.get_model(
                      'clip-tokenizer',
                       model_type=SDModelType.Tokenizer
                      )

This will raise an InvalidModelError if the format defined in the
config file doesn't match the requested model type.

MODELS.YAML

The general format of a models.yaml section is:

 type-of-model/name-of-model:
     path: /path/to/local/file/or/directory
     description: a description
     format: folder|ckpt|safetensors|pt
     base: SD-1|SD-2
     subfolder: subfolder-name

The type of model is given in the stanza key, and is one of
{diffusers, ckpt, vae, text_encoder, tokenizer, unet, scheduler,
safety_checker, feature_extractor, lora, textual_inversion,
controlnet}, and correspond to items in the SDModelType enum defined
in model_cache.py

The format indicates whether the model is organized as a folder with
model subdirectories, or is contained in a single checkpoint or
safetensors file.

One, but not both, of repo_id and path are provided. repo_id is the
HuggingFace repository ID of the model, and path points to the file or
directory on disk.

If subfolder is provided, then the model exists in a subdirectory of
the main model. These are usually named after the model type, such as
"unet".

This example summarizes the two ways of getting a non-diffuser model:

 text_encoder/clip-test-1:
   format: folder
   path: /path/to/folder
   description: Returns standalone CLIPTextModel

 text_encoder/clip-test-2:
   format: folder
   repo_id: /path/to/folder
   subfolder: text_encoder
   description: Returns the text_encoder in the subfolder of the diffusers model (just the encoder in RAM)

SUBMODELS:

It is also possible to fetch an isolated submodel from a diffusers
model. Use the `submodel` parameter to select which part:

 vae = manager.get_model('stable-diffusion-1.5',submodel=SDModelType.Vae)
 with vae.context as my_vae:
    print(type(my_vae))
    # "AutoencoderKL"

DIRECTORY_SCANNING:

Loras, textual_inversion and controlnet models are usually not listed
explicitly in models.yaml, but are added to the in-memory data
structure at initialization time by scanning the models directory. The
in-memory data structure can be resynchronized by calling
`manager.scan_models_directory`.

DISAMBIGUATION:

You may wish to use the same name for a related family of models. To
do this, disambiguate the stanza key with the model and and format
separated by "/". Example:

 tokenizer/clip-large:
   format: tokenizer
   path: /path/to/folder
   description: Returns standalone tokenizer

 text_encoder/clip-large:
   format: text_encoder
   path: /path/to/folder
   description: Returns standalone text encoder

You can now use the `model_type` argument to indicate which model you
want:

 tokenizer = mgr.get('clip-large',model_type=SDModelType.Tokenizer)
 encoder = mgr.get('clip-large',model_type=SDModelType.TextEncoder)

OTHER FUNCTIONS:

Other methods provided by ModelManager support importing, editing,
converting and deleting models.

IMPORTANT CHANGES AND LIMITATIONS SINCE 2.3:

1. Only local paths are supported. Repo_ids are no longer accepted. This
simplifies the logic.

2. VAEs can't be swapped in and out at load time. They must be baked
into the model when downloaded or converted.

"""
from __future__ import annotations

import os
import re
import textwrap
import shutil
import traceback
from dataclasses import dataclass
from enum import Enum, auto
from packaging import version
from pathlib import Path
from typing import Callable, Dict, Optional, List, Tuple, Union, types
from shutil import rmtree

import safetensors
import safetensors.torch
import torch
from diffusers import AutoencoderKL
from diffusers.utils import is_safetensors_available
from huggingface_hub import scan_cache_dir
from omegaconf import OmegaConf
from omegaconf.dictconfig import DictConfig

import invokeai.backend.util.logging as logger
from invokeai.app.services.config import InvokeAIAppConfig
from invokeai.backend.util import CUDA_DEVICE, download_with_resume
from ..install.model_install_backend import Dataset_path, hf_download_with_resume
from .model_cache import (ModelCache, ModelLocker, SDModelType,
                          SilenceWarnings)
# We are only starting to number the config file with release 3.
# The config file version doesn't have to start at release version, but it will help
# reduce confusion.
CONFIG_FILE_VERSION='3.0.0'

# wanted to use pydantic here, but Generator objects not supported
@dataclass
class SDModelInfo():
    context: ModelLocker
    name: str
    type: SDModelType
    hash: str
    location: Union[Path,str]
    precision: torch.dtype
    revision: str = None
    _cache: ModelCache = None

    def __enter__(self):
        return self.context.__enter__()

    def __exit__(self,*args, **kwargs):
        self.context.__exit__(*args, **kwargs)

class InvalidModelError(Exception):
    "Raised when an invalid model is requested"
    pass

class SDLegacyType(Enum):
    V1 = auto()
    V1_INPAINT = auto()
    V2 = auto()
    V2_e = auto()
    V2_v = auto()
    UNKNOWN = auto()

MAX_CACHE_SIZE = 6.0  # GB


# layout of the models directory:
# models
# ├── SD-1
# │   ├── controlnet
# │   ├── lora
# │   ├── diffusers
# │   └── textual_inversion
# ├── SD-2
# │   ├── controlnet
# │   ├── lora
# │   ├── diffusers
# │   └── textual_inversion
# └── support
#     ├── codeformer
#     ├── gfpgan
#     └── realesrgan


class ModelManager(object):
    """
    High-level interface to model management.
    """

    logger: types.ModuleType = logger

    def __init__(
        self,
        config: Union[Path, DictConfig, str],
        device_type: torch.device = CUDA_DEVICE,
        precision: torch.dtype = torch.float16,
        max_cache_size=MAX_CACHE_SIZE,
        sequential_offload=False,
        logger: types.ModuleType = logger,
    ):
        """
        Initialize with the path to the models.yaml config file. 
        Optional parameters are the torch device type, precision, max_models,
        and sequential_offload boolean. Note that the default device
        type and precision are set up for a CUDA system running at half precision.
        """
        if isinstance(config, DictConfig):
            self.config_path = None
            self.config = config
        elif isinstance(config,(str,Path)):
            self.config_path = config
            self.config = OmegaConf.load(self.config_path)
        else:
            raise ValueError('config argument must be an OmegaConf object, a Path or a string')

        # check config version number and update on disk/RAM if necessary
        self.globals = InvokeAIAppConfig.get_config()
        self._update_config_file_version()
        self.logger = logger
        self.cache = ModelCache(
            max_cache_size=max_cache_size,
            execution_device = device_type,
            precision = precision,
            sequential_offload = sequential_offload,
            logger = logger,
        )
        self.cache_keys = dict()

        # add controlnet, lora and textual_inversion models from disk
        self.scan_models_directory(include_diffusers=False)

    def model_exists(
        self,
        model_name: str,
        model_type: SDModelType = SDModelType.Diffusers,
    ) -> bool:
        """
        Given a model name, returns True if it is a valid
        identifier.
        """
        model_key = self.create_key(model_name, model_type)
        return model_key in self.config

    def create_key(self, model_name: str, model_type: SDModelType) -> str:
        return f"{model_type}/{model_name}"

    def parse_key(self, model_key: str) -> Tuple[str, SDModelType]:
        model_type_str, model_name = model_key.split('/', 1)
        try:
            model_type = SDModelType(model_type_str)
            return (model_name, model_type)
        except:
            raise Exception(f"Unknown model type: {model_type_str}")

    def get_model(
        self,
        model_name: str,
        model_type: SDModelType = SDModelType.Diffusers,
        submodel: Optional[SDModelType] = None,
    ) -> SDModelInfo:
        """Given a model named identified in models.yaml, return
        an SDModelInfo object describing it.
        :param model_name: symbolic name of the model in models.yaml
        :param model_type: SDModelType enum indicating the type of model to return
        :param submodel: an SDModelType enum indicating the portion of 
               the model to retrieve (e.g. SDModelType.Vae)

        If not provided, the model_type will be read from the `format` field
        of the corresponding stanza. If provided, the model_type will be used
        to disambiguate stanzas in the configuration file. The default is to
        assume a diffusers pipeline. The behavior is illustrated here:

        [models.yaml]
        diffusers/test1:
           repo_id: foo/bar
           description: Typical diffusers pipeline

        lora/test1:
           repo_id: /tmp/loras/test1.safetensors
           description: Typical lora file

        test1_pipeline = mgr.get_model('test1')
        # returns a StableDiffusionGeneratorPipeline

        test1_vae1 = mgr.get_model('test1', submodel=SDModelType.Vae)
        # returns the VAE part of a diffusers model as an AutoencoderKL

        test1_vae2 = mgr.get_model('test1', model_type=SDModelType.Diffusers, submodel=SDModelType.Vae)
        # does the same thing as the previous  statement. Note that model_type
        # is for the parent model, and submodel is for the part

        test1_lora = mgr.get_model('test1', model_type=SDModelType.Lora)
        # returns a LoRA embed (as a 'dict' of tensors)

        test1_encoder = mgr.get_modelI('test1', model_type=SDModelType.TextEncoder)
        # raises an InvalidModelError

        """
        model_key = self.create_key(model_name, model_type)
        if model_key not in self.config:
            raise InvalidModelError(
                f'"{model_key}" is not a known model name. Please check your models.yaml file'
            )
        
        # get the required loading info out of the config file
        mconfig = self.config[model_key]
        
        # type already checked as it's part of key
        if model_type == SDModelType.Diffusers:
            # intercept stanzas that point to checkpoint weights and replace them
            # with the equivalent diffusers model
            if mconfig.format in ["ckpt", "safetensors"]:
                location = self.convert_ckpt_and_cache(mconfig)
            elif mconfig.get('path'):
                location = self.globals.root_dir / mconfig.get('path')
            else:
                location = mconfig.get('repo_id')
        elif p := mconfig.get('path'):
            location = self.globals.root_dir / p
        elif r := mconfig.get('repo_id'):
            location = r
        elif w := mconfig.get('weights'):
            location = self.globals.root_dir / w
        else:
            location = None
        
        revision = mconfig.get('revision')
        if model_type in [SDModelType.Lora, SDModelType.TextualInversion]:
            hash = "<NO_HASH>" # TODO:
        else:
            hash = self.cache.model_hash(location, revision)

        # If the caller is asking for part of the model and the config indicates
        # an external replacement for that field, then we fetch the replacement
        if submodel and mconfig.get(submodel):
            location = mconfig.get(submodel).get('path') \
                or mconfig.get(submodel).get('repo_id')
            model_type = submodel
            submodel = None

        # to support the traditional way of attaching a VAE
        # to a model, we hacked in `attach_model_part`
        # TODO:
        if model_type == SDModelType.Vae and "vae" in mconfig:
            print("NOT_IMPLEMENTED - RETURN CUSTOM VAE")

        model_context = self.cache.get_model(
            location,
            model_type = model_type,
            revision = revision,
            submodel = submodel,
        )

        # in case we need to communicate information about this
        # model to the cache manager, then we need to remember
        # the cache key
        self.cache_keys[model_key] = model_context.key
        
        return SDModelInfo(
            context = model_context,
            name = model_name,
            type = submodel or model_type,
            hash = hash,
            location = location,
            revision = revision,
            precision = self.cache.precision,
            _cache = self.cache
        )

    def default_model(self) -> Optional[Tuple[str, SDModelType]]:
        """
        Returns the name of the default model, or None
        if none is defined.
        """
        for model_name, model_type in self.model_names():
            model_key = self.create_key(model_name, model_type)
            if self.config[model_key].get("default"):
                return (model_name, model_type)
        return self.model_names()[0][0]

    def set_default_model(self, model_name: str, model_type: SDModelType=SDModelType.Diffusers) -> None:
        """
        Set the default model. The change will not take
        effect until you call model_manager.commit()
        """
        assert self.model_exists(model_name, model_type), f"unknown model '{model_name}'"

        config = self.config
        for model_name, model_type in self.model_names():
            key = self.create_key(model_name, model_type)
            config[key].pop("default", None)
        config[self.create_key(model_name, model_type)]["default"] = True

    def model_info(
        self,
        model_name: str,
        model_type: SDModelType=SDModelType.Diffusers,
    ) -> dict:
        """
        Given a model name returns the OmegaConf (dict-like) object describing it.
        """
        if not self.model_exists(model_name, model_type):
            return None
        return self.config[self.create_key(model_name, model_type)]

    def model_names(self) -> List[Tuple[str, SDModelType]]:
        """
        Return a list of (str, SDModelType) corresponding to all models 
        known to the configuration.
        """
        return [(self.parse_key(x)) for x in self.config.keys() if isinstance(self.config[x], DictConfig)]

    def is_legacy(self, model_name: str, model_type: SDModelType.Diffusers) -> bool:
        """
        Return true if this is a legacy (.ckpt) model
        """
        # if we are converting legacy files automatically, then
        # there are no legacy ckpts!
        if self.globals.ckpt_convert:
            return False
        info = self.model_info(model_name, model_type)
        if "weights" in info and info["weights"].endswith((".ckpt", ".safetensors")):
            return True
        return False

    def list_models(self, model_type: SDModelType=None) -> dict[str,dict[str,str]]:
        """
        Return a dict of models, in format [model_type][model_name], with
        following fields:
               model_name
               model_type
               format
               description
               status
               # for folders only
               repo_id
               path
               subfolder
               vae
               # for ckpts only
               config
               weights
               vae

        Please use model_manager.models() to get all the model names,
        model_manager.model_info('model-name') to get the stanza for the model
        named 'model-name', and model_manager.config to get the full OmegaConf
        object derived from models.yaml
        """
        models = {}
        for model_key in sorted(self.config, key=str.casefold):
            stanza = self.config[model_key]
            # don't include VAEs in listing (legacy style)
            if "config" in stanza and "/VAE/" in stanza["config"]:
                continue
            if model_key.startswith('_'):
                continue

            model_name, stanza_type = self.parse_key(model_key)
            if model_type is not None and model_type != stanza_type:
                continue

            if stanza_type not in models:
                models[stanza_type] = dict()

            models[stanza_type][model_name] = dict()

            model_format = stanza.get('format')

            # Common Attribs
            description = stanza.get("description", None)
            models[stanza_type][model_name].update(
                model_name=model_name,
                model_type=stanza_type,
                format=model_format,
                description=description,
                status="unknown", # TODO: no more status as model loaded separately
            )

            # Checkpoint Config Parse
            if model_format in ["ckpt","safetensors"]:
                models[stanza_type][model_name].update(
                    config  = str(stanza.get("config", None)),
                    weights = str(stanza.get("weights", None)),
                    vae     = str(stanza.get("vae", None)),
                )

            # Diffusers Config Parse
            elif model_format == "folder":
                if vae := stanza.get("vae", None):
                    if isinstance(vae, DictConfig):
                        vae = dict(
                            repo_id   = str(vae.get("repo_id", None)),
                            path      = str(vae.get("path", None)),
                            subfolder = str(vae.get("subfolder", None)),
                        )

                models[stanza_type][model_name].update(
                    vae     = vae,
                    repo_id = str(stanza.get("repo_id", None)),
                    path    = str(stanza.get("path", None)),
                )

        return models

    def print_models(self) -> None:
        """
        Print a table of models, their descriptions, and load status
        """
        for model_type, model_dict in self.list_models().items():
            for model_name, model_info in model_dict.items():
                line = f'{model_info["model_name"]:25s} {model_info["status"]:>15s}  {model_info["model_type"]:10s} {model_info["description"]}'
                if model_info["status"] in ["in gpu","locked in gpu"]:
                    line = f"\033[1m{line}\033[0m"
                print(line)

    def del_model(
        self,
        model_name: str,
        model_type: SDModelType.Diffusers,
        delete_files: bool = False,
    ):
        """
        Delete the named model.
        """
        model_key = self.create_key(model_name, model_type)
        model_cfg = self.pop(model_key, None)

        if model_cfg is None:
            self.logger.error(
                f"Unknown model {model_key}"
            )
            return

        # TODO: some legacy?
        #if model_name in self.stack:
        #    self.stack.remove(model_name)

        if delete_files:
            repo_id = model_cfg.get("repo_id", None)
            path    = self._abs_path(model_cfg.get("path", None))
            weights = self._abs_path(model_cfg.get("weights", None))
            if "weights" in model_cfg:
                weights = self._abs_path(model_cfg["weights"])
                self.logger.info(f"Deleting file {weights}")
                Path(weights).unlink(missing_ok=True)

            elif "path" in model_cfg:
                path = self._abs_path(model_cfg["path"])
                self.logger.info(f"Deleting directory {path}")
                rmtree(path, ignore_errors=True)

            elif "repo_id" in model_cfg:
                repo_id = model_cfg["repo_id"]
                self.logger.info(f"Deleting the cached model directory for {repo_id}")
                self._delete_model_from_cache(repo_id)

    def add_model(
        self,
        model_name: str,
        model_type: SDModelType,
        model_attributes: dict,
        clobber: bool = False,
    ) -> None:
        """
        Update the named model with a dictionary of attributes. Will fail with an
        assertion error if the name already exists. Pass clobber=True to overwrite.
        On a successful update, the config will be changed in memory and the
        method will return True. Will fail with an assertion error if provided
        attributes are incorrect or the model name is missing.
        """

        if model_type == SDModelType.Fiffusers:
            # TODO: automaticaly or manualy?
            #assert "format" in model_attributes, 'missing required field "format"'
            model_format = "ckpt" if "weights" in model_attributes else "diffusers"

            if model_format == "diffusers":
                assert (
                    "description" in model_attributes
                ), 'required field "description" is missing'
                assert (
                    "path" in model_attributes or "repo_id" in model_attributes
                ), 'model must have either the "path" or "repo_id" fields defined'

            elif model_format == "ckpt":
                for field in ("description", "weights", "config"):
                    assert field in model_attributes, f"required field {field} is missing"

        else:
            assert "weights" in model_attributes and "description" in model_attributes

        model_key = self.create_key(model_name, model_type)

        assert (
            clobber or model_key not in self.config
        ), f'attempt to overwrite existing model definition "{model_key}"'

        self.config[model_key] = model_attributes

        if "weights" in self.config[model_key]:
            self.config[model_key]["weights"].replace("\\", "/")
            
        if clobber and model_key in self.cache_keys:
            self.cache.uncache_model(self.cache_keys[model_key])
            del self.cache_keys[model_key]

    def import_diffuser_model(
        self,
        repo_or_path: Union[str, Path],
        model_name: str = None,
        description: str = None,
        vae: dict = None,
        commit_to_conf: Path = None,
    ) -> bool:
        """
        Attempts to install the indicated diffuser model and returns True if successful.

        "repo_or_path" can be either a repo-id or a path-like object corresponding to the
        top of a downloaded diffusers directory.

        You can optionally provide a model name and/or description. If not provided,
        then these will be derived from the repo name. If you provide a commit_to_conf
        path to the configuration file, then the new entry will be committed to the
        models.yaml file.
        """
        model_name = model_name or Path(repo_or_path).stem
        model_description = description or f"Imported diffusers model {model_name}"
        new_config = dict(
            description=model_description,
            vae=vae,
            format="diffusers",
        )
        if isinstance(repo_or_path, Path) and repo_or_path.exists():
            new_config.update(path=str(repo_or_path))
        else:
            new_config.update(repo_id=repo_or_path)

        self.add_model(model_name, SDModelType.Diffusers, new_config, True)
        if commit_to_conf:
            self.commit(commit_to_conf)
        return self.create_key(model_name, SDModelType.Diffusers)

    def import_lora(
        self,
        path: Path,
        model_name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """
        Creates an entry for the indicated lora file. Call
        mgr.commit() to write out the configuration to models.yaml
        """
        path = Path(path)
        model_name = model_name or path.stem
        model_description = description or f"LoRA model {model_name}"
        self.add_model(
            model_name,
            SDModelType.Lora,
            dict(
                format="lora",
                weights=str(path),
                description=model_description,
            ),
            True
        )
        
    def import_embedding(
        self,
        path: Path,
        model_name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """
        Creates an entry for the indicated lora file. Call
        mgr.commit() to write out the configuration to models.yaml
        """
        path = Path(path)
        if path.is_directory() and (path / "learned_embeds.bin").exists():
            weights = path / "learned_embeds.bin"
        else:
            weights = path
            
        model_name = model_name or path.stem
        model_description = description or f"Textual embedding model {model_name}"
        self.add_model(
            model_name,
            SDModelType.TextualInversion,
            dict(
                format="textual_inversion",
                weights=str(weights),
                description=model_description,
            ),
            True
        )

    @classmethod
    def probe_model_type(self, checkpoint: dict) -> SDLegacyType:
        """
        Given a pickle or safetensors model object, probes contents
        of the object and returns an SDLegacyType indicating its
        format. Valid return values include:
        SDLegacyType.V1
        SDLegacyType.V1_INPAINT
        SDLegacyType.V2     (V2 prediction type unknown)
        SDLegacyType.V2_e   (V2 using 'epsilon' prediction type)
        SDLegacyType.V2_v   (V2 using 'v_prediction' prediction type)
        SDLegacyType.UNKNOWN
        """
        global_step = checkpoint.get("global_step")
        state_dict = checkpoint.get("state_dict") or checkpoint

        try:
            key_name = "model.diffusion_model.input_blocks.2.1.transformer_blocks.0.attn2.to_k.weight"
            if key_name in state_dict and state_dict[key_name].shape[-1] == 1024:
                if global_step == 220000:
                    return SDLegacyType.V2_e
                elif global_step == 110000:
                    return SDLegacyType.V2_v
                else:
                    return SDLegacyType.V2
            # otherwise we assume a V1 file
            in_channels = state_dict[
                "model.diffusion_model.input_blocks.0.0.weight"
            ].shape[1]
            if in_channels == 9:
                return SDLegacyType.V1_INPAINT
            elif in_channels == 4:
                return SDLegacyType.V1
            else:
                return SDLegacyType.UNKNOWN
        except KeyError:
            return SDLegacyType.UNKNOWN

    def heuristic_import(
        self,
        path_url_or_repo: str,
        model_name: Optional[str] = None,
        description: Optional[str] = None,
        model_config_file: Optional[Path] = None,
        commit_to_conf: Optional[Path] = None,
        config_file_callback: Optional[Callable[[Path], Path]] = None,
    ) -> str:
        """Accept a string which could be:
           - a HF diffusers repo_id
           - a URL pointing to a legacy .ckpt or .safetensors file
           - a local path pointing to a legacy .ckpt or .safetensors file
           - a local directory containing .ckpt and .safetensors files
           - a local directory containing a diffusers model

        After determining the nature of the model and downloading it
        (if necessary), the file is probed to determine the correct
        configuration file (if needed) and it is imported.

        The model_name and/or description can be provided. If not, they will
        be generated automatically.

        If commit_to_conf is provided, the newly loaded model will be written
        to the `models.yaml` file at the indicated path. Otherwise, the changes
        will only remain in memory.

        The routine will do its best to figure out the config file
        needed to convert legacy checkpoint file, but if it can't it
        will call the config_file_callback routine, if provided. The
        callback accepts a single argument, the Path to the checkpoint
        file, and returns a Path to the config file to use.

        The (potentially derived) name of the model is returned on
        success, or None on failure. When multiple models are added
        from a directory, only the last imported one is returned.

        """
        model_path: Path = None
        thing = str(path_url_or_repo)  # to save typing

        self.logger.info(f"Probing {thing} for import")

        if thing.startswith(("http:", "https:", "ftp:")):
            self.logger.info(f"{thing} appears to be a URL")
            model_path = self._resolve_path(
                thing, "models/ldm/stable-diffusion-v1"
            )  # _resolve_path does a download if needed

        elif Path(thing).is_file() and thing.endswith((".ckpt", ".safetensors")):
            if Path(thing).stem in ["model", "diffusion_pytorch_model"]:
                self.logger.debug(f"{Path(thing).name} appears to be part of a diffusers model. Skipping import")
                return
            else:
                self.logger.debug(f"{thing} appears to be a checkpoint file on disk")
                model_path = self._resolve_path(thing, "models/ldm/stable-diffusion-v1")

        elif Path(thing).is_dir() and Path(thing, "model_index.json").exists():
            self.logger.debug(f"{thing} appears to be a diffusers file on disk")
            model_name = self.import_diffuser_model(
                thing,
                vae=dict(repo_id="stabilityai/sd-vae-ft-mse"),
                model_name=model_name,
                description=description,
                commit_to_conf=commit_to_conf,
            )

        elif Path(thing).is_dir():
            if (Path(thing) / "model_index.json").exists():
                self.logger.debug(f"{thing} appears to be a diffusers model.")
                model_name = self.import_diffuser_model(
                    thing, commit_to_conf=commit_to_conf
                )
            else:
                self.logger.debug(f"{thing} appears to be a directory. Will scan for models to import")
                for m in list(Path(thing).rglob("*.ckpt")) + list(
                    Path(thing).rglob("*.safetensors")
                ):
                    if model_name := self.heuristic_import(
                            str(m),
                            commit_to_conf=commit_to_conf,
                            config_file_callback=config_file_callback,
                    ):
                        self.logger.info(f"{model_name} successfully imported")
                return model_name

        elif re.match(r"^[\w.+-]+/[\w.+-]+$", thing):
            self.logger.debug(f"{thing} appears to be a HuggingFace diffusers repo_id")
            model_name = self.import_diffuser_model(
                thing, commit_to_conf=commit_to_conf
            )
            pipeline, _, _, _ = self._load_diffusers_model(self.config[model_name])
            return model_name
        else:
            self.logger.warning(f"{thing}: Unknown thing. Please provide a URL, file path, directory or HuggingFace repo_id")

        # Model_path is set in the event of a legacy checkpoint file.
        # If not set, we're all done
        if not model_path:
            return

        if model_path.stem in self.config:  # already imported
            self.logger.debug("Already imported. Skipping")
            return model_path.stem

        # another round of heuristics to guess the correct config file.
        checkpoint = None
        if model_path.suffix in [".ckpt", ".pt"]:
            self.cache.scan_model(model_path, model_path)
            checkpoint = torch.load(model_path)
        else:
            checkpoint = safetensors.torch.load_file(model_path)

        # additional probing needed if no config file provided
        if model_config_file is None:
            # look for a like-named .yaml file in same directory
            if model_path.with_suffix(".yaml").exists():
                model_config_file = model_path.with_suffix(".yaml")
                self.logger.debug(f"Using config file {model_config_file.name}")

            else:
                model_type = self.probe_model_type(checkpoint)
                if model_type == SDLegacyType.V1:
                    self.logger.debug("SD-v1 model detected")
                    model_config_file = self.globals.legacy_conf_path / "v1-inference.yaml"
                elif model_type == SDLegacyType.V1_INPAINT:
                    self.logger.debug("SD-v1 inpainting model detected")
                    model_config_file = self.globals.legacy_conf_path / "v1-inpainting-inference.yaml"
                elif model_type == SDLegacyType.V2_v:
                    self.logger.debug("SD-v2-v model detected")
                    model_config_file = self.globals.legacy_conf_path / "v2-inference-v.yaml"
                elif model_type == SDLegacyType.V2_e:
                    self.logger.debug("SD-v2-e model detected")
                    model_config_file = self.globals.legacy_conf_path / "v2-inference.yaml"
                elif model_type == SDLegacyType.V2:
                    self.logger.warning(
                        f"{thing} is a V2 checkpoint file, but its parameterization cannot be determined."
                    )
                else:
                    self.logger.warning(
                        f"{thing} is a legacy checkpoint file but not a known Stable Diffusion model."
                    )

        if not model_config_file and config_file_callback:
            model_config_file = config_file_callback(model_path)

        # despite our best efforts, we could not find a model config file, so give up
        if not model_config_file:
            return

        # look for a custom vae, a like-named file ending with .vae in the same directory
        vae_path = None
        for suffix in ["pt", "ckpt", "safetensors"]:
            if (model_path.with_suffix(f".vae.{suffix}")).exists():
                vae_path = model_path.with_suffix(f".vae.{suffix}")
                self.logger.debug(f"Using VAE file {vae_path.name}")
        vae = None if vae_path else dict(repo_id="stabilityai/sd-vae-ft-mse")

        diffuser_path = self.globals.converted_ckpts_dir / model_path.stem
        with SilenceWarnings():
            model_name = self.convert_and_import(
                model_path,
                diffusers_path=diffuser_path,
                vae=vae,
                vae_path=str(vae_path),
                model_name=model_name,
                model_description=description,
                original_config_file=model_config_file,
                commit_to_conf=commit_to_conf,
                scan_needed=False,
            )
        return model_name

    def convert_ckpt_and_cache(self, mconfig: DictConfig) -> Path:
        """
        Convert the checkpoint model indicated in mconfig into a
        diffusers, cache it to disk, and return Path to converted
        file. If already on disk then just returns Path.
        """
        weights = self.globals.root_dir / mconfig.weights
        config_file = self.globals.root_dir / mconfig.config
        diffusers_path = self.globals.converted_ckpts_dir / weights.stem

        # return cached version if it exists
        if diffusers_path.exists():
            return diffusers_path

        vae_ckpt_path, vae_model = self._get_vae_for_conversion(weights, mconfig)

        # to avoid circular import errors
        from .convert_ckpt_to_diffusers import convert_ckpt_to_diffusers
        with SilenceWarnings():        
            convert_ckpt_to_diffusers(
                weights,
                diffusers_path,
                extract_ema=True,
                original_config_file=config_file,
                vae=vae_model,
                vae_path=str(self.globals.root_dir / vae_ckpt_path) if vae_ckpt_path else None,
                scan_needed=True,
            )
        return diffusers_path

    def convert_vae_ckpt_and_cache(self, mconfig: DictConfig) -> Path:
        """
        Convert the VAE indicated in mconfig into a diffusers AutoencoderKL
        object, cache it to disk, and return Path to converted
        file. If already on disk then just returns Path.
        """
        root = self.globals.root_dir
        weights_file = root / mconfig.weights
        config_file = root / mconfig.config
        diffusers_path = self.globals.converted_ckpts_dir / weights_file.stem
        image_size = mconfig.get('width') or mconfig.get('height') or 512
        
        # return cached version if it exists
        if diffusers_path.exists():
            return diffusers_path

        # this avoids circular import error
        from .convert_ckpt_to_diffusers import convert_ldm_vae_to_diffusers
        checkpoint =  torch.load(weights_file, map_location="cpu")\
            if weights_file.suffix in ['.ckpt','.pt'] \
               else  safetensors.torch.load_file(weights_file)

        # sometimes weights are hidden under "state_dict", and sometimes not
        if "state_dict" in checkpoint:
            checkpoint = checkpoint["state_dict"]

        config = OmegaConf.load(config_file)

        vae_model = convert_ldm_vae_to_diffusers(
            checkpoint = checkpoint,
            vae_config = config,
            image_size = image_size
        )
        vae_model.save_pretrained(
            diffusers_path,
            safe_serialization=is_safetensors_available()
        )
        return diffusers_path

    def _get_vae_for_conversion(
        self,
        weights: Path,
        mconfig: DictConfig
    ) -> Tuple[Path, AutoencoderKL]:
        # VAE handling is convoluted
        # 1. If there is a .vae.ckpt file sharing same stem as weights, then use
        # it as the vae_path passed to convert
        vae_ckpt_path = None
        vae_diffusers_location = None
        vae_model = None
        for suffix in ["pt", "ckpt", "safetensors"]:
            if (weights.with_suffix(f".vae.{suffix}")).exists():
                vae_ckpt_path = weights.with_suffix(f".vae.{suffix}")
                self.logger.debug(f"Using VAE file {vae_ckpt_path.name}")
        if vae_ckpt_path:
            return (vae_ckpt_path, None)
                
        # 2. If mconfig has a vae weights path, then we use that as vae_path
        vae_config = mconfig.get('vae')
        if vae_config and isinstance(vae_config,str):
            vae_ckpt_path = vae_config
            return (vae_ckpt_path, None)
            
        # 3. If mconfig has a vae dict, then we use it as the diffusers-style vae
        if vae_config and isinstance(vae_config,DictConfig):
            vae_diffusers_location = self.globals.root_dir / vae_config.get('path') \
                if vae_config.get('path') \
                   else vae_config.get('repo_id')

        # 4. Otherwise, we use stabilityai/sd-vae-ft-mse "because it works"
        else:
            vae_diffusers_location = "stabilityai/sd-vae-ft-mse"

        if vae_diffusers_location:
            vae_model = self.cache.get_model(vae_diffusers_location, SDModelType.Vae).model
            return (None, vae_model)

        return (None, None)
            
    def convert_and_import(
        self,
        ckpt_path: Path,
        diffusers_path: Path,
        model_name=None,
        model_description=None,
        vae: dict = None,
        vae_path: Path = None,
        original_config_file: Path = None,
        commit_to_conf: Path = None,
        scan_needed: bool = True,
    ) -> str:
        """
        Convert a legacy ckpt weights file to diffuser model and import
        into models.yaml.
        """
        ckpt_path = self._resolve_path(ckpt_path, "models/ldm/stable-diffusion-v1")
        if original_config_file:
            original_config_file = self._resolve_path(
                original_config_file, "configs/stable-diffusion"
            )

        new_config = None

        if diffusers_path.exists():
            self.logger.error(
                f"The path {str(diffusers_path)} already exists. Please move or remove it and try again."
            )
            return

        model_name = model_name or diffusers_path.name
        model_description = model_description or f"Converted version of {model_name}"
        self.logger.debug(f"Converting {model_name} to diffusers (30-60s)")

        # to avoid circular import errors
        from .convert_ckpt_to_diffusers import convert_ckpt_to_diffusers

        try:
            # By passing the specified VAE to the conversion function, the autoencoder
            # will be built into the model rather than tacked on afterward via the config file
            vae_model = None
            if vae:
                vae_location = self.globals.root_dir / vae.get('path') \
                    if vae.get('path') \
                       else vae.get('repo_id')
                vae_model = self.cache.get_model(vae_location, SDModelType.Vae).model
                vae_path = None
            convert_ckpt_to_diffusers(
                ckpt_path,
                diffusers_path,
                extract_ema=True,
                original_config_file=original_config_file,
                vae=vae_model,
                vae_path=vae_path,
                scan_needed=scan_needed,
            )
            self.logger.debug(
                f"Success. Converted model is now located at {str(diffusers_path)}"
            )
            self.logger.debug(f"Writing new config file entry for {model_name}")
            new_config = dict(
                path=str(diffusers_path),
                description=model_description,
                format="diffusers",
            )
            if self.model_exists(model_name, SDModelType.Diffusers):
                self.del_model(model_name, SDModelType.Diffusers)
            self.add_model(
                model_name,
                SDModelType.Diffusers,
                new_config,
                True
            )
            if commit_to_conf:
                self.commit(commit_to_conf)
            self.logger.debug(f"Model {model_name} installed")
        except Exception as e:
            self.logger.warning(f"Conversion failed: {str(e)}")
            self.logger.warning(traceback.format_exc())
            self.logger.warning(
                "If you are trying to convert an inpainting or 2.X model, please indicate the correct config file (e.g. v1-inpainting-inference.yaml)"
            )

        return model_name

    def search_models(self, search_folder):
        self.logger.info(f"Finding Models In: {search_folder}")
        models_folder_ckpt = Path(search_folder).glob("**/*.ckpt")
        models_folder_safetensors = Path(search_folder).glob("**/*.safetensors")

        ckpt_files = [x for x in models_folder_ckpt if x.is_file()]
        safetensor_files = [x for x in models_folder_safetensors if x.is_file()]

        files = ckpt_files + safetensor_files

        found_models = []
        for file in files:
            location = str(file.resolve()).replace("\\", "/")
            if (
                "model.safetensors" not in location
                and "diffusion_pytorch_model.safetensors" not in location
            ):
                found_models.append({"name": file.stem, "location": location})

        return search_folder, found_models

    def commit(self, conf_file: Path=None) -> None:
        """
        Write current configuration out to the indicated file.
        """
        yaml_str = OmegaConf.to_yaml(self.config)
        config_file_path = conf_file or self.config_path
        assert config_file_path is not None,'no config file path to write to'
        config_file_path = self.globals.root_dir / config_file_path
        tmpfile = os.path.join(os.path.dirname(config_file_path), "new_config.tmp")
        with open(tmpfile, "w", encoding="utf-8") as outfile:
            outfile.write(self.preamble())
            outfile.write(yaml_str)
        os.replace(tmpfile, config_file_path)

    def preamble(self) -> str:
        """
        Returns the preamble for the config file.
        """
        return textwrap.dedent(
            """\
            # This file describes the alternative machine learning models
            # available to InvokeAI script.
            #
            # To add a new model, follow the examples below. Each
            # model requires a model config file, a weights file,
            # and the width and height of the images it
            # was trained on.
        """
        )

    @classmethod
    def _delete_model_from_cache(cls,repo_id):
        cache_info = scan_cache_dir(InvokeAIAppConfig.get_config().cache_dir)

        # I'm sure there is a way to do this with comprehensions
        # but the code quickly became incomprehensible!
        hashes_to_delete = set()
        for repo in cache_info.repos:
            if repo.repo_id == repo_id:
                for revision in repo.revisions:
                    hashes_to_delete.add(revision.commit_hash)
        strategy = cache_info.delete_revisions(*hashes_to_delete)
        cls.logger.warning(
            f"Deletion of this model is expected to free {strategy.expected_freed_size_str}"
        )
        strategy.execute()

    @staticmethod
    def _abs_path(path: str | Path) -> Path:
        globals = InvokeAIAppConfig.get_config()
        if path is None or Path(path).is_absolute():
            return path
        return Path(globals.root_dir, path).resolve()

    # This is not the same as global_resolve_path(), which prepends
    # Globals.root.
    def _resolve_path(
        self, source: Union[str, Path], dest_directory: str
    ) -> Optional[Path]:
        resolved_path = None
        if str(source).startswith(("http:", "https:", "ftp:")):
            dest_directory = self.globals.root_dir / dest_directory
            dest_directory.mkdir(parents=True, exist_ok=True)
            resolved_path = download_with_resume(str(source), dest_directory)
        else:
            resolved_path = self.globals.root_dir / source
        return resolved_path

    def _update_config_file_version(self):
        """
        This gets called at object init time and will update
        from older versions of the config file to new ones
        as necessary.
        """
        current_version = self.config.get("_version","1.0.0")
        if version.parse(current_version) < version.parse(CONFIG_FILE_VERSION):
            self.logger.warning(f'models.yaml version {current_version} detected. Updating to {CONFIG_FILE_VERSION}')
            self.logger.warning('The original file will be renamed models.yaml.orig')
            if self.config_path:
                old_file = Path(self.config_path)
                new_name = old_file.parent / 'models.yaml.orig'
                old_file.replace(new_name)
            
            new_config = OmegaConf.create()
            new_config["_version"] = CONFIG_FILE_VERSION
            
            for model_key in self.config:

                old_stanza = self.config[model_key]
                if not isinstance(old_stanza,DictConfig):
                    continue

                # ignore old and ugly way of associating a legacy
                # vae with a legacy checkpont model
                if old_stanza.get("config") and '/VAE/' in old_stanza.get("config"):
                    continue

                # bare keys are updated to be prefixed with 'diffusers/'
                if '/' not in model_key:
                    new_key = f'diffusers/{model_key}'
                else:
                    new_key = model_key

                if old_stanza.get('format')=='diffusers':
                    model_format = 'folder'
                elif old_stanza.get('weights') and Path(old_stanza.get('weights')).suffix == '.ckpt':
                    model_format = 'ckpt'
                elif old_stanza.get('weights') and Path(old_stanza.get('weights')).suffix == '.safetensors':
                    model_format = 'safetensors'
                else:
                    model_format = old_stanza.get('format')

                # copy fields over manually rather than doing a copy() or deepcopy()
                # in order to avoid bringing in unwanted fields.
                new_config[new_key] = dict(
                    description = old_stanza.get('description'),
                    format = model_format,
                )
                for field in ["repo_id", "path", "weights", "config", "vae"]:
                    if field_value :=  old_stanza.get(field):
                        new_config[new_key].update({field: field_value})
            
            self.config = new_config
            if self.config_path:
                self.commit()

    def _delete_defunct_models(self):
        '''
        Remove models no longer on disk.
        '''
        config = self.config
        
        to_delete = set()
        for key in config:
            if 'path' not in config[key]:
                continue
            path = self.globals.root_dir / config[key].path
            if path.exists():
                continue
            to_delete.add(key)
            
        for key in to_delete:
            self.logger.warn(f'Removing model {key} from in-memory config because its path is no longer on disk')
            config.pop(key)

    def scan_models_directory(self, include_diffusers:bool=False):
        '''
        Scan the models directory for loras, textual_inversions and controlnets
        and create appropriate entries in the in-memory omegaconf. Diffusers
        will not be added unless include_diffusers is true.
        '''
        self._delete_defunct_models()
        
        model_directory = self.globals.models_path
        config = self.config
        
        for root, dirs, files in os.walk(model_directory):
            parents = root.split('/')
            subpaths = parents[parents.index('models')+1:]
            if len(subpaths) < 2:
                continue
            base, model_type, *_ = subpaths
            
            if model_type == "diffusers" and not include_diffusers:
                continue
            
            for d in dirs:
                config[f'{model_type}/{d}'] = dict(
                    path = os.path.join(root,d),
                    description = f'{model_type} model {d}',
                    format = 'folder',
                    base = base,
                )

            for f in files:
                basename = Path(f).stem
                format = Path(f).suffix[1:]
                config[f'{model_type}/{basename}'] = dict(
                    path = os.path.join(root,f),
                    description = f'{model_type} model {basename}',
                    format = format,
                    base = base,
                )
                
        
    ##### NONE OF THE METHODS BELOW WORK NOW BECAUSE OF MODEL DIRECTORY REORGANIZATION
    ##### AND NEED TO BE REWRITTEN
    def list_lora_models(self)->Dict[str,bool]:
        '''Return a dict of installed lora models; key is either the shortname
        defined in INITIAL_MODELS, or the basename of the file in the LoRA
        directory. Value is True if installed'''

        models = OmegaConf.load(Dataset_path).get('lora') or {}
        installed_models = {x: False for x in models.keys()}
        
        dir = self.globals.lora_path
        installed_models = dict()
        for root, dirs, files in os.walk(dir):
            for name in files:
                if Path(name).suffix not in ['.safetensors','.ckpt','.pt','.bin']:
                    continue
                if name == 'pytorch_lora_weights.bin':
                    name = Path(root,name).parent.stem #Path(root,name).stem
                else:
                    name = Path(name).stem
                installed_models.update({name: True})

        return installed_models
    
    def install_lora_models(self, model_names: list[str], access_token:str=None):
        '''Download list of LoRA/LyCORIS models'''
        
        short_names = OmegaConf.load(Dataset_path).get('lora') or {}
        for name in model_names:
            name = short_names.get(name) or name

            # HuggingFace style LoRA
            if re.match(r"^[\w.+-]+/([\w.+-]+)$", name):
                self.logger.info(f'Downloading LoRA/LyCORIS model {name}')
                _,dest_dir = name.split("/")
                
                hf_download_with_resume(
                    repo_id = name,
                    model_dir = self.globals.lora_path / dest_dir,
                    model_name = 'pytorch_lora_weights.bin',
                    access_token = access_token,
                )

            elif name.startswith(("http:", "https:", "ftp:")):
                download_with_resume(name, self.globals.lora_path)

            else:
                self.logger.error(f"Unknown repo_id or URL: {name}")
    
    def delete_lora_models(self, model_names: List[str]):
        '''Remove the list of lora models'''
        for name in model_names:
            file_or_directory = self.globals.lora_path / name
            if file_or_directory.is_dir():
                self.logger.info(f'Purging LoRA/LyCORIS {name}')
                shutil.rmtree(str(file_or_directory))
            else:
                for path in self.globals.lora_path.glob(f'{name}.*'):
                    self.logger.info(f'Purging LoRA/LyCORIS {name}')
                    path.unlink()
                
    def list_ti_models(self)->Dict[str,bool]:
        '''Return a dict of installed textual models; key is either the shortname
        defined in INITIAL_MODELS, or the basename of the file in the LoRA
        directory. Value is True if installed'''

        models = OmegaConf.load(Dataset_path).get('textual_inversion') or {}
        installed_models = {x: False for x in models.keys()}
        
        dir = self.globals.embedding_path
        for root, dirs, files in os.walk(dir):
            for name in files:
                if not Path(name).suffix in ['.bin','.pt','.ckpt','.safetensors']:
                    continue
                if name == 'learned_embeds.bin':
                    name = Path(root,name).parent.stem #Path(root,name).stem
                else:
                    name = Path(name).stem
                installed_models.update({name: True})
        return installed_models

    def install_ti_models(self, model_names: list[str], access_token: str=None):
        '''Download list of textual inversion embeddings'''

        short_names = OmegaConf.load(Dataset_path).get('textual_inversion') or {}
        for name in model_names:
            name = short_names.get(name) or name
            
            if re.match(r"^[\w.+-]+/([\w.+-]+)$", name):
                self.logger.info(f'Downloading Textual Inversion embedding {name}')
                _,dest_dir = name.split("/")
                hf_download_with_resume(
                    repo_id = name,
                    model_dir = self.globals.embedding_path / dest_dir,
                    model_name = 'learned_embeds.bin',
                    access_token = access_token
                )
            elif name.startswith(('http:','https:','ftp:')):
                download_with_resume(name, self.globals.embedding_path)
            else:
                self.logger.error(f'{name} does not look like either a HuggingFace repo_id or a downloadable URL')
    
    def delete_ti_models(self, model_names: list[str]):
        '''Remove TI embeddings from disk'''
        for name in model_names:
            file_or_directory = self.globals.embedding_path / name
            if file_or_directory.is_dir():
                self.logger.info(f'Purging textual inversion embedding {name}')
                shutil.rmtree(str(file_or_directory))
            else:
                for path in self.globals.embedding_path.glob(f'{name}.*'):
                    self.logger.info(f'Purging textual inversion embedding {name}')
                    path.unlink()

    def list_controlnet_models(self)->Dict[str,bool]:
        '''Return a dict of installed controlnet models; key is repo_id or short name
        of model (defined in INITIAL_MODELS), and value is True if installed'''

        cn_models = OmegaConf.load(Dataset_path).get('controlnet') or {}
        installed_models = {x: False for x in cn_models.keys()}
        
        cn_dir = self.globals.controlnet_path
        for root, dirs, files in os.walk(cn_dir):
            for name in dirs:
                if Path(root, name, '.download_complete').exists():
                    installed_models.update({name.replace('--','/'): True})
        return installed_models

    def install_controlnet_models(self, model_names: list[str], access_token: str=None):
        '''Download list of controlnet models; provide either repo_id or short name listed in INITIAL_MODELS.yaml'''
        short_names = OmegaConf.load(Dataset_path).get('controlnet') or {}
        dest_dir = self.globals.controlnet_path
        dest_dir.mkdir(parents=True,exist_ok=True)
        
        # The model file may be fp32 or fp16, and may be either a
        # .bin file or a .safetensors. We try each until we get one,
        # preferring 'fp16' if using half precision, and preferring
        # safetensors over over bin.
        precisions = ['.fp16',''] if self.precision=='float16' else ['']
        formats = ['.safetensors','.bin']
        possible_filenames = list()
        for p in precisions:
            for f in formats:
                possible_filenames.append(Path(f'diffusion_pytorch_model{p}{f}'))
        
        for directory_name in model_names:
            repo_id = short_names.get(directory_name) or directory_name
            safe_name = directory_name.replace('/','--')
            self.logger.info(f'Downloading ControlNet model {directory_name} ({repo_id})')
            hf_download_with_resume(
                repo_id = repo_id,
                model_dir = dest_dir / safe_name,
                model_name = 'config.json',
                access_token = access_token
            )

            path = None
            for filename in possible_filenames:
                suffix = filename.suffix
                dest_filename = Path(f'diffusion_pytorch_model{suffix}')
                self.logger.info(f'Checking availability of {directory_name}/{filename}...')
                path = hf_download_with_resume(
                    repo_id = repo_id,
                    model_dir = dest_dir / safe_name,
                    model_name = str(filename),
                    access_token = access_token,
                    model_dest = Path(dest_dir, safe_name, dest_filename),
                )
                if path:
                    (path.parent / '.download_complete').touch()
                    break

    def delete_controlnet_models(self, model_names: List[str]):
        '''Remove the list of controlnet models'''
        for name in model_names:
            safe_name = name.replace('/','--')
            directory = self.globals.controlnet_path / safe_name
            if directory.exists():
                self.logger.info(f'Purging controlnet model {name}')
                shutil.rmtree(str(directory))

