# Changelog

## 0.5.1

- Add refresh_image option to FeedConfig and update related functionality

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
