# The API, as a container.
#
# Two things here are not the usual shape, and both are deliberate:
#
#   * **The segmenter is opt-in.** `WITH_SEGMENTER=1` installs TotalSegmentator
#     and bakes in its weights; the default is off, because App Platform's own
#     documented limits make the full install unsafe to host there. See below.
#
#   * **The build context is `apps/api`**, so the `.dockerignore` beside this file
#     is load-bearing: without it the local development store (`.data`, gigabytes
#     of DICOM) would be uploaded into every build.
#
# Run locally:
#     docker build -t bone-viewer-api apps/api
#     docker run -p 8080:8080 -e BONE_VIEWER_STORAGE=local bone-viewer-api
#
# With the segmenter:
#     docker build --build-arg WITH_SEGMENTER=1 -t bone-viewer-api apps/api

FROM python:3.12-slim

# uv, so the install is the one uv.lock describes rather than whatever pip
# resolves on the day.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    # Never fetch a private Python: the image already has the interpreter the
    # project asks for, and a surprise download is a surprise build length.
    UV_PYTHON_DOWNLOADS=never \
    # No wheel cache. The build container has a fixed disk budget and this
    # install is large; nothing is rebuilt within a single build, so the cache
    # is weight with no benefit.
    UV_NO_CACHE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    # Read by TotalSegmentator itself for the weights location.
    TOTALSEG_HOME_DIR=/opt/totalseg

WORKDIR /srv

# Dependencies first, on their own layer: they change far less often than the
# code.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# ── the segmenter, off by default ───────────────────────────────────────────
#
# On, this is what makes real segmentation work. Off, the app is unaffected in
# every other respect: `/segmenter/classes` reports `available: false` with an
# actionable message, a `segment` request against a DICOM source fails with the
# install hint rather than silently doing something else, and everything that is
# not segmentation behaves normally.
#
# It is off because hosting it on App Platform fights two documented limits, and
# neither can be raised:
#
#   * App Platform gives every build 8 GB of RAM and disk, and image sizes above
#     2 GiB are documented as "likely to experience build and deployment issues".
#     This project plus CPU PyTorch plus the weights is roughly 3-3.7 GB. The
#     first build of this image failed with BuildJobTerminated ("resource
#     exhaustion") while installing the *default* PyTorch wheel, which is the
#     CUDA build -- about 2.5 GB of `nvidia-*` libraries and triton that a CPU
#     container cannot use. That is fixed below, but the weights are the larger
#     half of the problem and there is no version of this that fits comfortably.
#
#   * A running container's local filesystem is capped at 4 GiB, and a full one
#     is "detected as unhealthy and replaced". Baked weights would consume most
#     of that, and `SourceStore.materialize` still has to stage a source's DICOM
#     onto the same filesystem before the segmenter can read it -- for the study
#     this was built against, several gigabytes on its own. A container that
#     trips that limit is replaced, in a loop.
#
# A machine with a real disk is the right home for this: a Droplet, or a build
# pushed to a registry from outside App Platform. Until then, off.
ARG WITH_SEGMENTER=0

# CPU PyTorch first, and from the CPU index only. The default wheel on PyPI is
# the CUDA build; it is what exhausted the first build, and none of it is usable
# here.
RUN if [ "$WITH_SEGMENTER" = "1" ]; then \
      uv pip install --python /opt/venv/bin/python \
        --index-url https://download.pytorch.org/whl/cpu \
        torch torchvision \
      && uv pip install --python /opt/venv/bin/python TotalSegmentator \
      && python -c "import torch; assert torch.version.cuda is None, \
           'a CUDA build of torch got in; the build container cannot hold it'" \
      && totalseg_download_weights -t total \
      && totalseg_download_weights -t total_fast \
      && chown -R 10001:10001 /opt/totalseg ; \
    else \
      echo "segmenter disabled; build with --build-arg WITH_SEGMENTER=1 to include it" ; \
    fi

COPY app ./app

# Nothing in the image needs to be root, but everything needs to be writable by
# whatever runs it: the staging directory `SourceStore.materialize` needs for the
# segmenter, and the local-store fallback. Owning them is cheaper than
# discovering a permission problem on the first upload.
RUN useradd --create-home --uid 10001 viewer \
 && mkdir -p /opt/totalseg \
 && chown -R viewer:viewer /srv /opt/totalseg
USER viewer

# App Platform routes to the port the service declares, and also exports PORT.
# Honouring both is what stops the declared port and the listening port drifting
# apart, which presents as a deploy that succeeds and a hostname that never
# answers.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]

