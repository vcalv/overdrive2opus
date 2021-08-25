# overdrive2opus
Convert overdrive audiobooks into a single opus file with chapter and thumbnail information

For downloading overdrive audiobooks check out https://github.com/chbrown/overdrive

# Features

* Thumbnail embeding
* Lower bitrate reencoding (default is 15 Kbps, 20 Kbps gives excellent results)
* Chapter information retrieved from proprietary overdrive metadada
* Can ignore spurious subchapters that exist in many audiobooks
* Speedup of audio (with chapter adjustment)

# Dependencies

* python3
* ffmpeg
* opusenc
* python-progress (optional)
