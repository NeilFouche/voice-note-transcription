# vendor/

`fluent-web-components.min.js` is Microsoft's [Fluent UI Web Components](https://github.com/microsoft/fluentui/tree/master/packages/web-components)
(`@fluentui/web-components` + `@fluentui/tokens`, MIT licensed), bundled into a
single offline file - app.py reads it at runtime and inlines it into the
page (pywebview serves the UI as an in-memory HTML string, so there's no
web server for a CDN-style `<script src>` to fetch from, and this app has
to work without internet access anyway).

It's a real, Microsoft-maintained component library (`<fluent-radio>`,
`<fluent-button>`, `<fluent-field>`, etc. render with the actual Fluent
Design System, the same one Windows 11's own apps use) - not hand-rolled
CSS trying to imitate native Windows controls.

## Regenerating it

Needed after bumping the Fluent UI version, or if the bundle is lost.
Requires Node.js.

```sh
mkdir /tmp/fluent-build && cd /tmp/fluent-build
npm init -y
npm install @fluentui/web-components@3.1.3
npm install --no-save esbuild

cat > entry.js <<'EOF'
import '@fluentui/web-components/web-components-all.js';
import { setTheme } from '@fluentui/web-components';
import { webLightTheme, webDarkTheme } from '@fluentui/tokens';

function applyTheme() {
  const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  setTheme(dark ? webDarkTheme : webLightTheme);
}

applyTheme();
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
EOF

node_modules/.bin/esbuild entry.js --bundle --minify --format=iife \
  --outfile=fluent-bundle.min.js
```

Copy the result to `vendor/fluent-web-components.min.js`.

## Design tokens used in app.py

`setTheme()` applies Fluent's tokens as CSS custom properties on `<html>`
(light/dark switched automatically via `prefers-color-scheme`), so
app.py's own CSS references them directly instead of hard-coded colors,
e.g. `var(--colorNeutralBackground1)` (card surfaces),
`var(--colorNeutralForeground2)` (secondary text), `var(--colorBrandForeground1)`
(accent). See the [Fluent 2 token reference](https://github.com/microsoft/fluentui/tree/master/packages/tokens)
for the full list.
