"""Both navigation map pairs must survive a clean checkout without local files."""
from pathlib import Path

from PIL import Image
import pytest
import yaml

MAPS = Path(__file__).resolve().parents[1] / 'maps'


@pytest.mark.parametrize('name', ['tb3_sandbox', 'proposed_system'])
def test_saved_map_has_readable_image(name):
    config = yaml.safe_load((MAPS / f'{name}.yaml').read_text())
    image = MAPS / config['image']
    assert image.is_file(), f'Missing map image in checkout: {image}'
    with Image.open(image) as raster:
        raster.load()
        assert raster.width > 0 and raster.height > 0
    assert config['resolution'] > 0
    assert 0 <= config['free_thresh'] < config['occupied_thresh'] <= 1
