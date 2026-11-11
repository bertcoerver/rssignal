# Changelog

## Unreleased

- rssignal no longer downloads anything. The fetchit add-on reads the feeds and
  files what is new in /media/fetchit; rssignal sends from there. Move each
  feed's `url`, and the filters that decide what is worth downloading, to a
  feed of the same name in fetchit.json — rssignal says so if it finds a `url`.
- Removed: the media archive (`media_keep_days`; fetchit's library replaces it),
  `update_yt_dlp`, and yt-dlp itself. New option: `library`.
- Group pictures come from the library and are checked after every run, rather
  than fetched from the source about once a month.
- A panel in Home Assistant's sidebar: status, every feed and where it stands,
  run and dry-run buttons, a send form, recent activity, the log, and linking
  by QR code.
- Notifications from Home Assistant: its Signal Messenger integration works with
  rssignal as its `url`, and `/api/send` takes JSON from anything else. New
  options `api_allow` and `api_token`.
- The schedule is kept by `rssignal serve`, which holds one signal-cli for the
  add-on's whole life, so a notification never waits behind a run.

## 0.5.0

- Enhance documentation for video playback in Infuse and add support for series metadata in NPO feed parsing
- Enhance GitHub Actions workflow to support automatic merging of patch releases and improve pull request handling
- Add GitHub Actions workflow to automate signal-cli version bumping

## 0.4.1

- Refactor URL handling to ensure safe encoding for downloads and improve error handling
- Add URL handling improvements for downloads and enhance error handling

## 0.4.0

- Add support for NPO Start series and improve video handling in downloads
- Add VideoGone exception for expired videos and update documentation

## Unreleased

- NPO Start series can be followed: point a feed at
  `https://npo.nl/start/serie/<series>/afleveringen` (newest season) or a single
  season. Episodes are fetched through downloadgemist.nl, with its own pause
  between downloads respected and failing episodes backed off.
- Anything too big for one Signal message — video from any source, or a long
  podcast episode — is now sent in as few parts as possible, at the best quality
  that fits that many, instead of being skipped. The media archive keeps the whole
  file.

## 0.3.0

- First release installed from the add-on repository.

## 0.2.0

- Installed as a local add-on, built on the host from a copied `addon/` folder.
