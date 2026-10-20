# Race Carousel Maker

A static web page that turns a batch of photos into a seamless Instagram carousel.
Photos are laid out on one long panorama and sliced into 1080 px slides, so shots
can run across the swipe from one slide to the next.

- **Mosaic**: tiled grid on a solid background, tiles crossing the slide seams.
- **Layered**: the same tidy grid on top of blurred copies of your photos.
- Formats 4:5, 3:4 and 1:1, up to 20 slides.
- Tap a photo for its toolbar: resize, crop zoom, remove. Tap two photos to swap.
- Drag a photo to move its crop, drag the gaps between photos to resize them.
- Export as a ZIP of numbered JPEGs, a single slide, or the phone share sheet.

Everything runs in the browser; no photo is uploaded anywhere.

## Run locally

Open `index.html` through any static server, e.g. `python3 -m http.server` in this folder.

## Publish

`.github/workflows/carousel.yml` deploys this folder to GitHub Pages on every push
to `master` that touches it. In the repo settings set **Pages → Source** to
**GitHub Actions** once.
