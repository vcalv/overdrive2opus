# overdrive2opus
Convert overdrive audiobooks into a single opus file with chapter and thumbnail information

For downloading overdrive audiobooks check out https://github.com/chbrown/overdrive

# Features

* Thumbnail embedding
* Lower bit-rate re-encoding (default is 15 Kbps, 20 Kbps gives excellent results)
* Chapter information retrieved from proprietary overdrive metadata
* Can ignore spurious sub-chapters that exist in many audiobooks
* Speedup of audio (with chapter adjustment)
* Audio (peak one-pass) normalization

# Dependencies

* python3
* ffmpeg
* opusenc
* python-progress (optional)
