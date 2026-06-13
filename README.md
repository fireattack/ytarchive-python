# ytarchive

YouTube livestream archiver — downloads live streams in real-time by fetching individual fragments and muxing with ffmpeg.

Python rewrite of [Kethsar/ytarchive](https://github.com/Kethsar/ytarchive) and its [dreammu fork](https://github.com/dreammu/ytarchive). Uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) for stream metadata extraction.

## Dependencies

- Python 3.9+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [ffmpeg](https://ffmpeg.org/)

```
pip install -r requirements.txt
```

## Usage

```
python ytarchive.py [OPTIONS] [url] [quality]
```

See `python ytarchive.py --help` for the full option list.

## License

See [LICENSE](LICENSE).
