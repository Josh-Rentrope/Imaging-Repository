# The API, as a container.
#
# Two things here are not the usual shape, and both are deliberate:
#
#   * **TotalSegmentator's weights are downloaded at build time.** App Platform
#     replaces containers rather than restarting them, so fetching several
#     gigabytes on first use would repeat on every deploy and every scaling
#     event, and no segmentation could start until it finished.
#
#   * **The build context is `apps/api`**, so the `.dockerignore` beside this file
#     is load-bearing: without it the local development store (`.data`, gigabytes
#     of DICOM) would be uploaded into every build.
#
# Run locally:
#     docker build -t bone-viewer-api apps/api
#     docker run -p 8080:8080 -e BONE_VIEWER_STORAGE=local bone-viewer-api

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
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    # Read by TotalSegmentator itself for the weights location. Set before the
    # download so the build and the run cannot disagree about where they are.
    TOTALSEG_HOME_DIR=/opt/totalseg

WORKDIR /srv

# Dependencies first, on their own layer: they change far less often than the
# code, and PyTorch is the bulk of the install.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Installed here rather than declared in pyproject, because it is shelled out to
# and never imported -- the app runs the command. See app/pipeline/totalseg.py.
RUN uv pip install --python /opt/venv/bin/python TotalSegmentator

# `total` is the task the app names by default. `total_fast` is what `fast=True`
# resolves to, and fast is the app's default, so both are needed for the
# out-of-the-box path. Any other task downloads on first use, which is slow but
# correct; baking all of them would be tens of gigabytes.
RUN totalseg_download_weights -t total \
 && totalseg_download_weights -t total_fast

COPY app ./app

# Nothing in the image needs to be root, but everything needs to be writable by
# whatever runs it: the staging directory `SourceStore.materialize` needs for the
# segmenter, the weights, and the local-store fallback. Owning them is cheaper
# than discovering a permission problem on the first upload.
RUN useradd --create-home --uid 10001 viewer \
 && chown -R viewer:viewer /srv /opt/totalseg
USER viewer

# App Platform routes to the port the service declares, and also exports PORT.
# Honouring both is what stops the declared port and the listening port drifting
# apart, which presents as a deploy that succeeds and a hostname that never
# answers.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
