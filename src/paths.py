"""Filesystem layout helpers (repo root, pictures, models, Label Studio)."""

import os
from functools import lru_cache


@lru_cache(maxsize=1)
def kittyhack_root() -> str:
    """Absolute path to the kittyhack repository root (directory containing `src/`)."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@lru_cache(maxsize=1)
def install_base() -> str:
    """Parent of the repo root (or ``KITTYHACK_INSTALL_BASE``), for sibling data dirs."""
    # Allow explicit override (useful for custom layouts)
    override = os.environ.get("KITTYHACK_INSTALL_BASE")
    if override:
        return os.path.abspath(override)

    return os.path.abspath(os.path.join(kittyhack_root(), ".."))


def pictures_root() -> str:
    """Root directory for stored event pictures."""
    return os.path.join(install_base(), "pictures")


def pictures_original_dir() -> str:
    """Directory for full-size original images."""
    return os.path.join(pictures_root(), "original_images")


def pictures_thumbnails_dir() -> str:
    """Directory for thumbnail images."""
    return os.path.join(pictures_root(), "thumbnails")


def models_yolo_root() -> str:
    """Directory for YOLO model files."""
    return os.path.join(install_base(), "models", "yolo")


def wallpapers_dir() -> str:
    """07.10, Sid ("choisir parmi des wallpaper, ou télécharger depuis
    smartphone - comme pour meelo"): small gallery of uploaded background
    images for the React pages, same idea as Meelo's custom-wallpaper
    picker. Lives outside the repo root (install_base()) so it survives a
    git-based update the same way pictures/ and models/ already do."""
    return os.path.join(install_base(), "wallpapers")
