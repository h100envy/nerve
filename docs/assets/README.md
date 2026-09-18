# Assets

`nerve_banner.png` is rendered from `nerve_banner.html`, so it can be
regenerated whenever the protocol changes (a node added, a claim reworded)
instead of being redrawn by hand.

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 \
  --screenshot=docs/assets/nerve_banner.png \
  --window-size=1200,400 \
  "file://$PWD/docs/assets/nerve_banner.html"
```

The source is 1200×400 logical pixels; the scale factor renders it at 2400×800
so it stays sharp on a retina display. It uses only system fonts and inline
SVG, so the render needs no network access.
