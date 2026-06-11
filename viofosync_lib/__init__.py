"""Public API for Viofo dashcam sync helpers.

Split into three private submodules by responsibility:

- :mod:`viofosync_lib._archive` — filename patterns, path helpers,
  filesystem walking
- :mod:`viofosync_lib._protocol` — HTTP API to the dashcam (XML
  listing, HTML scrape, byte downloader)
- :mod:`viofosync_lib._gpx` — MP4 atom parsing + GPX generation
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.request

# Re-export the public API.
from ._archive import (
    Recording,
    downloaded_filename_re,
    get_downloaded_recordings,
    get_filepath,
    get_group_name,
)
from ._gpx import (
    extract_gps_data,
    generate_gpx,
    parse_moov,
)
from ._protocol import (
    DownloadCancelled,
    download_file,
    get_dashcam_filenames,
    get_dashcam_filenames_html,
)
from .progress import ProgressSink


def download_file_with(
    *args,
    max_attempts: int | None = None,
    socket_timeout: float | None = None,
    **kwargs,
):
    """Call :func:`download_file` with per-call ``max_attempts`` /
    ``socket_timeout`` overrides. Passes them straight through as
    parameters (download_file resolves None to the module defaults),
    so two concurrent downloads never clobber each other's settings."""
    from . import _protocol as _proto
    return _proto.download_file(
        *args,
        max_attempts=max_attempts,
        socket_timeout=socket_timeout,
        **kwargs,
    )


def delete_dashcam_file(
    base_url: str,
    source_dir: str,
    filename: str,
    *,
    timeout: float = 10.0,
) -> bool:
    """Ask the Viofo dashcam to delete ``<source_dir>/<filename>``.

    Confirmed protocol against the A229 Pro:

        GET <base_url>/?custom=1&cmd=4003&str=<absolute-path>

    Returns True on a 2xx response, False on any HTTP, URL, or
    timeout error. Never raises — failure is the caller's cue to
    log a warning and continue.
    """
    log = logging.getLogger("viofosync_lib.delete")
    # source_dir already includes the leading slash on the dashcam
    # (e.g. "/DCIM/Movie") and never has a trailing slash; build the
    # absolute path with a single join.
    path = f"{source_dir}/{filename}"
    url = f"{base_url}/?custom=1&cmd=4003&str={path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            ok = 200 <= getattr(resp, "status", 0) < 300
            if not ok:
                log.warning(
                    "dashcam delete %s: HTTP %s", filename, resp.status
                )
            return ok
    except urllib.error.HTTPError as e:
        log.warning("dashcam delete %s: HTTP %s", filename, e.code)
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("dashcam delete %s: %s", filename, e)
        return False


__all__ = [
    "DownloadCancelled",
    "Recording",
    "ProgressSink",
    "delete_dashcam_file",
    "download_file",
    "download_file_with",
    "downloaded_filename_re",
    "extract_gps_data",
    "generate_gpx",
    "get_dashcam_filenames",
    "get_dashcam_filenames_html",
    "get_downloaded_recordings",
    "get_filepath",
    "get_group_name",
    "parse_moov",
]
