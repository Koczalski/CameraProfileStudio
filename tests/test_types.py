from dataclasses import FrozenInstanceError
import numpy as np
import pytest
from camera_profile_studio.types import (SpaceSpec, DcpSettings, ImageData, JobContext, MatchSettings,
                                         SamplePairs, RawRecipe, Progress)
from camera_profile_studio.errors import ValidationError, CancelledError, checkpoint


def test_image_owns_immutable_copy():
    source = np.zeros((2, 3, 3), dtype=np.float32)
    image = ImageData(source, None, metadata=[('source', 'fixture')])
    source[:] = 1
    assert not image.rgb.any()
    assert image.rgb.flags.c_contiguous and not image.rgb.flags.writeable
    assert image.metadata == (('source', 'fixture'),)
    with pytest.raises(ValueError):
        image.rgb.setflags(write=True)
    with pytest.raises(FrozenInstanceError):
        image.source_path = None


@pytest.mark.parametrize('factory', [lambda: SpaceSpec('unknown'), lambda: SpaceSpec('icc_rgb'),
    lambda: SpaceSpec('srgb', 'bad.icc'), lambda: DcpSettings(camera_neutral=(1, 0, 1)),
    lambda: DcpSettings(illuminant_kelvin=float('nan')), lambda: ImageData(np.zeros((3, 3)), None),
    lambda: MatchSettings(holdout_fraction=1), lambda: MatchSettings(regularization=0),
    lambda: RawRecipe(use_camera_wb=False), lambda: RawRecipe(user_wb=(1, 1, 1, 1)),
    lambda: Progress('bad', float('nan'))])
def test_invalid_records(factory):
    with pytest.raises(ValidationError):
        factory()


def test_sample_shape_and_evidence_validation():
    with pytest.raises(ValidationError):
        SamplePairs(np.zeros((2, 3)), np.zeros((3, 3)), ('x', 'y'), 'chart')
    with pytest.raises(ValidationError):
        SamplePairs(np.zeros((2, 3)), np.zeros((2, 3)), ('x', 'y'), '')


def test_cancel_before_progress():
    class Cancel:
        def is_cancelled(self):
            return True
    updates = []
    with pytest.raises(CancelledError) as failure:
        checkpoint(JobContext(updates.append, Cancel()), stage='test', fraction=0)
    assert failure.value.code == 'cancelled' and updates == []
    checkpoint(JobContext(updates.append), stage='test', fraction=0.5)
    assert updates == [Progress('test', 0.5)]
