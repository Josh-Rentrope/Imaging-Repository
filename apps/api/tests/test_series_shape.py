"""Is this series a stack of slices, or a set of flat images?

Both ways of getting it wrong are silent: stacking frames that advance in time
invents a z axis, and refusing projections loses the study.
"""

from __future__ import annotations

import io
import json
import math

import numpy as np
import pytest
from helpers import run  # noqa: E402  (tests/ is on sys.path under pytest)
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid


def upload_dicom(client, payloads: list[tuple[str, bytes]], **form):
    """Local, not imported from test_smoke: that would couple the two modules."""
    return client.post(
        "/sources/dicom",
        files=[("files", (name, data, "application/dicom")) for name, data in payloads],
        data={"workspace_id": "ws-1", "set_id": "set-1", **form},
    )


def make_projection(
    index: int = 0,
    series_uid: str | None = None,
    *,
    rows: int = 10,
    cols: int = 12,
    spacing: list[float] | None = None,
    spacing_tag: str | None = "ImagerPixelSpacing",
    orientation: list[float] | None = None,
    position: list[float] | None = None,
    modality: str = "DX",
    frames: int = 1,
) -> bytes:
    """One flat acquisition: no position or `PixelSpacing` unless asked for."""
    dataset = Dataset()
    dataset.file_meta = FileMetaDataset()
    dataset.file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    dataset.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    dataset.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = dataset.file_meta.MediaStorageSOPInstanceUID
    dataset.StudyInstanceUID = generate_uid()
    dataset.SeriesInstanceUID = series_uid or generate_uid()
    dataset.Modality = modality
    dataset.InstanceNumber = index + 1

    if spacing is not None and spacing_tag is not None:
        setattr(dataset, spacing_tag, spacing)
    if orientation is not None:
        dataset.ImageOrientationPatient = orientation
    if position is not None:
        dataset.ImagePositionPatient = position

    dataset.Rows = rows
    dataset.Columns = cols
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.WindowCenter = 128
    dataset.WindowWidth = 256
    if frames > 1:
        dataset.NumberOfFrames = frames
        dataset.PixelData = np.full((frames, rows, cols), 100 + index, dtype=np.uint16).tobytes()
    else:
        dataset.PixelData = np.full((rows, cols), 100 + index, dtype=np.uint16).tobytes()

    buffer = io.BytesIO()
    dataset.save_as(buffer, enforce_file_format=True)
    return buffer.getvalue()


# -- a lone image is a plane ------------------------------------------------


def test_a_single_slice_is_a_plane_not_a_one_slice_volume(client):
    """One slice has no third axis, so nothing should claim it does."""
    series_uid = generate_uid()
    source = upload_dicom(
        client, [("only.dcm", make_projection(series_uid=series_uid, spacing=[0.2, 0.2]))]
    ).json()

    assert source["presentation"] == "plane"
    assert source["plane_count"] == 1
    assert source["renderable"] is True
    # Not a failure, so no failure text: the client shows this as a description.
    assert "not a stack" in source["render_reason"]


def test_a_plane_source_has_no_volume_but_does_have_a_drawable_image(client):
    """Both halves matter: no volume, and something to look at."""
    source = upload_dicom(
        client, [("only.dcm", make_projection(spacing=[0.2, 0.2]))]
    ).json()
    source_id = source["source_id"]

    assert client.get(f"/sources/{source_id}/volume").status_code == 409

    listing = client.get(f"/sources/{source_id}/images").json()
    assert listing["axis"] == "plane"
    assert listing["count"] == 1

    image = client.get(f"/sources/{source_id}/images/0")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG")


# -- spacing comes from more than one place ---------------------------------


@pytest.mark.parametrize(
    "tag",
    ["PixelSpacing", "ImagerPixelSpacing", "NominalScannedPixelSpacing"],
)
def test_every_spacing_tag_is_reached_for(client, tag):
    """A radiograph keeps its spacing in `ImagerPixelSpacing`."""
    source = upload_dicom(
        client, [("x.dcm", make_projection(spacing=[0.15, 0.2], spacing_tag=tag))]
    ).json()

    image = client.get(f"/sources/{source['source_id']}/images").json()["images"][0]
    assert image["spacing_source"] == tag
    # [x, y], not DICOM's [row, column]: rows are 0.15, columns 0.2.
    assert image["spacing"] == [0.2, 0.15]


def test_ultrasound_spacing_comes_from_the_region_sequence(client):
    """An ultrasound carries its scale only in the region sequence."""
    dataset = Dataset()
    dataset.file_meta = FileMetaDataset()
    dataset.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    dataset.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = dataset.file_meta.MediaStorageSOPInstanceUID
    dataset.StudyInstanceUID = generate_uid()
    dataset.SeriesInstanceUID = generate_uid()
    dataset.Modality = "US"
    dataset.Rows = 8
    dataset.Columns = 8
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"

    region = Dataset()
    region.PhysicalDeltaX = 0.0203
    region.PhysicalDeltaY = 0.0203
    dataset.SequenceOfUltrasoundRegions = [region]
    dataset.PixelData = np.full((8, 8), 100, dtype=np.uint16).tobytes()

    buffer = io.BytesIO()
    dataset.save_as(buffer, enforce_file_format=True)

    source = upload_dicom(client, [("u.dcm", buffer.getvalue())]).json()
    image = client.get(f"/sources/{source['source_id']}/images").json()["images"][0]
    assert image["spacing_source"] == "UltrasoundRegion"
    assert image["spacing"] == pytest.approx([0.0203, 0.0203])


def test_an_image_with_no_spacing_anywhere_says_so_rather_than_guessing(client):
    """No spacing anywhere reports null rather than a guess."""
    source = upload_dicom(client, [("m.dcm", make_projection(spacing=None))]).json()
    image = client.get(f"/sources/{source['source_id']}/images").json()["images"][0]
    assert image["spacing"] is None
    assert image["spacing_source"] is None
    # Still drawable. An image of unknown size is not an image to refuse.
    assert client.get(f"/sources/{source['source_id']}/images/0").status_code == 200


# -- frames are not slices --------------------------------------------------


def test_frames_with_no_slice_position_are_not_stacked(client):
    """Frames that advance in time must not be stacked into a volume."""
    source = upload_dicom(
        client,
        [("cardiac.dcm", make_projection(spacing=[4.8, 4.8], frames=6, modality="NM"))],
    ).json()

    assert source["presentation"] == "plane"
    assert source["plane_count"] == 6
    assert client.get(f"/sources/{source['source_id']}/volume").status_code == 409
    assert "advance in time" in source["render_reason"]


def test_each_frame_is_listed_and_reachable(client):
    source = upload_dicom(
        client, [("loop.dcm", make_projection(spacing=[0.2, 0.2], frames=4))]
    ).json()
    source_id = source["source_id"]

    listing = client.get(f"/sources/{source_id}/images").json()
    assert [i["index"] for i in listing["images"]] == [0, 1, 2, 3]
    assert listing["images"][0]["frame"] == 0
    assert client.get(f"/sources/{source_id}/images/3").status_code == 200
    assert client.get(f"/sources/{source_id}/images/4").status_code == 404


def test_indexes_span_files_and_frames_together(client):
    """Two files of two frames are four images, not two."""
    series_uid = generate_uid()
    source = upload_dicom(
        client,
        [
            ("a.dcm", make_projection(0, series_uid, spacing=[0.2, 0.2], frames=2)),
            ("b.dcm", make_projection(1, series_uid, spacing=[0.2, 0.2], frames=2)),
        ],
    ).json()

    assert source["plane_count"] == 4
    listing = client.get(f"/sources/{source['source_id']}/images").json()
    assert [(i["index"], i["frame"]) for i in listing["images"]] == [
        (0, 0),
        (1, 1),
        (2, 0),
        (3, 1),
    ]


def test_a_projection_keeps_its_own_orientation(client):
    """A lateral view is not the same plane as the frontal one beside it."""
    series_uid = generate_uid()
    source = upload_dicom(
        client,
        [
            (
                "a-pa.dcm",
                make_projection(
                    0,
                    series_uid,
                    spacing=[0.2, 0.2],
                    orientation=[1, 0, 0, 0, 1, 0],
                    position=[0, 0, 0],
                ),
            ),
            (
                "b-lat.dcm",
                make_projection(
                    1,
                    series_uid,
                    spacing=[0.2, 0.2],
                    orientation=[0, 1, 0, 0, 0, -1],
                    position=[100, 0, 0],
                ),
            ),
        ],
    ).json()

    images = client.get(f"/sources/{source['source_id']}/images").json()["images"]
    assert [i["name"] for i in images] == ["a-pa.dcm", "b-lat.dcm"]
    assert images[0]["direction"] != images[1]["direction"]
    assert images[0]["origin"] == [0.0, 0.0, 0.0]
    assert images[1]["origin"] == [100.0, 0.0, 0.0]


# -- repeats in one series --------------------------------------------------


def test_slices_repeated_at_one_position_are_dropped_not_stretched(client):
    """Repeated positions are dropped, not stacked into a deeper volume."""
    series_uid = generate_uid()
    payloads = []
    for repeat in range(3):
        for slice_index in range(4):
            payloads.append(
                (
                    f"e{repeat}s{slice_index}.dcm",
                    make_projection(
                        slice_index,
                        series_uid,
                        modality="MR",
                        spacing=[1.0, 1.0],
                        spacing_tag="PixelSpacing",
                        orientation=[1, 0, 0, 0, 1, 0],
                        position=[0.0, 0.0, float(slice_index) * 3.0],
                    ),
                )
            )

    source = upload_dicom(client, payloads).json()
    assert source["presentation"] == "volume"
    assert source["dropped_slices"] == 8
    assert source["instance_count"] == 12

    header = client.get(f"/sources/{source['source_id']}/volume").json()["header"]
    # Four slices 3 mm apart, not twelve stacked back to back.
    assert header["dims"][2] == 4
    assert header["spacing"][2] == pytest.approx(3.0)


def test_an_evenly_spaced_series_stays_a_volume(client):
    """The ordinary case must not be caught by the disparity check."""
    series_uid = generate_uid()
    payloads = [
        (
            f"s{index}.dcm",
            make_projection(
                index,
                series_uid,
                modality="CT",
                spacing=[0.7, 0.7],
                spacing_tag="PixelSpacing",
                orientation=[1, 0, 0, 0, 1, 0],
                position=[0.0, 0.0, float(index) * 1.25],
            ),
        )
        for index in range(8)
    ]
    source = upload_dicom(client, payloads).json()

    assert source["presentation"] == "volume"
    assert source["coherent"] is True
    assert 1.2 < source["coherence"]["gap_mm"] < 1.3


def test_one_missing_slice_does_not_condemn_the_study(client):
    """One missing slice does not condemn the study."""
    series_uid = generate_uid()
    # 0..9 but skipping 4, so the gap at that point is 2mm instead of 1mm.
    positions = [index for index in range(10) if index != 4]
    payloads = [
        (
            f"s{index}.dcm",
            make_projection(
                index,
                series_uid,
                modality="CT",
                spacing=[0.7, 0.7],
                spacing_tag="PixelSpacing",
                orientation=[1, 0, 0, 0, 1, 0],
                position=[0.0, 0.0, float(position)],
            ),
        )
        for index, position in enumerate(positions)
    ]
    source = upload_dicom(client, payloads).json()

    assert source["presentation"] == "volume"
    assert source["coherent"] is True


def test_slices_at_random_heights_are_not_a_volume(client):
    """Irregular gaps mean a folder of unrelated images, not an acquisition."""
    series_uid = generate_uid()
    heights = [0.0, 37.0, 41.5, 90.0, 91.2, 150.0]
    payloads = [
        (
            f"s{index}.dcm",
            make_projection(
                index,
                series_uid,
                modality="CT",
                spacing=[0.7, 0.7],
                spacing_tag="PixelSpacing",
                orientation=[1, 0, 0, 0, 1, 0],
                position=[0.0, 0.0, height],
            ),
        )
        for index, height in enumerate(heights)
    ]
    source = upload_dicom(client, payloads).json()

    assert source["presentation"] == "plane"
    assert source["coherent"] is False
    assert "not evenly spaced" in source["render_reason"]
    # Still drawable, as the images it actually is.
    assert source["renderable"] is True
    assert client.get(f"/sources/{source['source_id']}/images").json()["count"] == len(heights)


def test_slices_at_different_angles_are_not_a_volume(client):
    """Two acquisitions at different angles are two acquisitions, whatever UID they share."""
    series_uid = generate_uid()
    payloads = []
    for index in range(6):
        # The last three are rotated 30 degrees about z.
        angle = 0.0 if index < 3 else 30.0
        radians = math.radians(angle)
        row = [round(math.cos(radians), 6), round(math.sin(radians), 6), 0]
        col = [round(-math.sin(radians), 6), round(math.cos(radians), 6), 0]
        payloads.append(
            (
                f"s{index}.dcm",
                make_projection(
                    index,
                    series_uid,
                    modality="CT",
                    spacing=[0.7, 0.7],
                    spacing_tag="PixelSpacing",
                    orientation=[*row, *col],
                    position=[0.0, 0.0, float(index)],
                ),
            )
        )
    source = upload_dicom(client, payloads).json()

    assert source["presentation"] == "plane"
    assert source["coherent"] is False
    assert "same angle" in source["render_reason"]


# -- records written before the analysis existed ----------------------------


def _age_the_record(client, source_id: str) -> None:
    """Make a record look like one written before the re-analysis existed."""
    store = client.app.state.source_store
    record = store.get(source_id)
    assert record is not None
    raw = record.model_dump(mode="json")
    for field in ("presentation", "plane_count", "spacing_source", "dropped_slices",
                  "coherent", "coherence", "analysis_version"):
        raw.pop(field, None)
    # An old study that "rendered": the blob exists, the verdict does not.
    raw["renderable"] = True
    raw["render_reason"] = None
    store.storage.put(store._index_key(source_id), json.dumps(raw).encode("utf-8"))


def test_a_record_from_before_the_analysis_is_reanalysed_on_read(client):
    """A study sliced by an older build is fixed on read, not stranded."""
    source = upload_dicom(
        client, [("x.dcm", make_projection(spacing=[0.2, 0.2]))]
    ).json()
    source_id = source["source_id"]
    _age_the_record(client, source_id)

    # Selected: the list is what the client hydrates from, so it has to fix it.
    listed = client.get("/sources").json()
    recovered = next(item for item in listed if item["source_id"] == source_id)

    assert recovered["presentation"] == "plane"
    assert recovered["plane_count"] == 1
    assert recovered["renderable"] is True
    # And it is stamped, so the next read does not do it again.
    assert client.app.state.source_store.get(source_id).analysis_version >= 2


def test_reanalysis_rewrites_a_blob_the_new_rules_would_shrink(client):
    """The stored blob is rebuilt when the verdict changed, not reused."""
    series_uid = generate_uid()
    payloads = []
    for repeat in range(3):
        for slice_index in range(4):
            payloads.append(
                (
                    f"e{repeat}s{slice_index}.dcm",
                    make_projection(
                        slice_index,
                        series_uid,
                        modality="MR",
                        spacing=[1.0, 1.0],
                        spacing_tag="PixelSpacing",
                        orientation=[1, 0, 0, 0, 1, 0],
                        position=[0.0, 0.0, float(slice_index) * 3.0],
                    ),
                )
            )
    source = upload_dicom(client, payloads).json()
    assert source["dropped_slices"] == 8

    _age_the_record(client, source["source_id"])
    response = client.post(f"/sources/{source['source_id']}/reanalyse")
    assert response.status_code == 200

    rebuilt = response.json()
    assert rebuilt["presentation"] == "volume"
    assert rebuilt["dropped_slices"] == 8
    header = client.get(f"/sources/{source['source_id']}/volume").json()["header"]
    assert header["dims"][2] == 4


def test_reanalysing_an_unknown_source_is_a_404(client):
    assert client.post("/sources/nope/reanalyse").status_code == 404


def test_declared_slice_spacing_makes_a_stack_without_positions(client):
    """Declared slice spacing makes a stack without positions."""
    series_uid = generate_uid()
    payloads = []
    for slice_index in range(5):
        data = make_projection(
            slice_index, series_uid, modality="MG", spacing=[0.085, 0.085]
        )
        payloads.append((f"t{slice_index}.dcm", data))

    import pydicom

    # InstanceNumber is all a position-less stack can be ordered by.
    rebuilt = []
    for name, data in payloads:
        dataset = pydicom.dcmread(io.BytesIO(data))
        dataset.SpacingBetweenSlices = 1.0
        buffer = io.BytesIO()
        dataset.save_as(buffer, enforce_file_format=True)
        rebuilt.append((name, buffer.getvalue()))

    source = upload_dicom(client, rebuilt).json()
    assert source["presentation"] == "volume"
    header = client.get(f"/sources/{source['source_id']}/volume").json()["header"]
    assert header["dims"][2] == 5


# -- ops that need a third axis --------------------------------------------


def test_segment_refuses_a_plane_and_names_an_op_that_takes_one(client):
    """A segmentation of the wrong axis is not obviously wrong to look at."""
    source = upload_dicom(
        client, [("x.dcm", make_projection(spacing=[0.2, 0.2]))]
    ).json()
    job = run(client, [{"op": "segment", "params": {}}], {"source_id": source["source_id"]})
    assert job["status"] == "failed"
    error = job["error"] or ""
    assert "not a stack" in error
    # Points at the op that does take a flat image, rather than a dead end.
    assert "px2tooth" in error


def test_a_stack_that_thins_to_one_slice_still_hits_the_voxel_guard():
    """The voxel guard still exists behind the classification."""
    from app.pipeline.isosurface import SurfaceError, extract

    volume = np.zeros((1, 8, 8), dtype="<f4")
    header = {"dims": [8, 8, 1], "spacing": [1.0, 1.0, 1.0], "origin": [0.0, 0.0, 0.0]}

    with pytest.raises(SurfaceError, match="at least 2 along every axis"):
        extract(volume.tobytes(), header, threshold=0.5)
