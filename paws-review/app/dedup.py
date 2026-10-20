import imagehash
from PIL import Image


def compute_phash(image_path: str) -> str:
    with Image.open(image_path) as img:
        return str(imagehash.phash(img))


def hamming_distance(hash_a: str, hash_b: str) -> int:
    return imagehash.hex_to_hash(hash_a) - imagehash.hex_to_hash(hash_b)
