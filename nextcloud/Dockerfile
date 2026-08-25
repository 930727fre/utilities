FROM nextcloud:apache

# libheif1 — decodes HEIC (iPhone photo default). Nextcloud's Imagick-
#            based OC\Preview\HEIC provider relies on this being present
#            in the system library path.
# ffmpeg   — generates video thumbnails for OC\Preview\Movie (.mov / .mp4
#            / .webm / etc). iPhone videos default to HEVC in .mov.
#
# Stock nextcloud:apache doesn't ship either — HEIC/MOV files upload
# fine but preview API returns generic icons, both web UI and iOS/Android
# app show blank thumbnails. Adding these makes the preview providers
# actually work; UX pain vs Immich still remains at the "photo browsing
# feel" layer (no face recognition, no map view, basic timeline) but
# thumbnails at least appear.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libheif1 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*
