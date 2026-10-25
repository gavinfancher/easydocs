#!/bin/sh
# Rebuild app/static/vendor/codemirror.min.js, the editor's text area, as one file that sets
# window.CM. Needs Node. Bump the pinned versions here to upgrade.
set -eu
out="$(cd "$(dirname "$0")/.." && pwd)/app/static/vendor/codemirror.min.js"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
npm init -y >/dev/null
npm install --silent --no-audit --no-fund esbuild@0.28.2 \
  @codemirror/state@6.7.6 @codemirror/view@6.43.14 @codemirror/commands@6.11.1 \
  @codemirror/language@6.13.1 @codemirror/lang-markdown@6.5.2 @codemirror/search@6.7.2 \
  @lezer/highlight@1.2.5
cat > entry.js <<'EOF'
export { EditorState, EditorSelection, StateEffect, Prec, RangeSetBuilder } from '@codemirror/state';
export { EditorView, Decoration, ViewPlugin, WidgetType, keymap, placeholder, drawSelection } from '@codemirror/view';
export { history, defaultKeymap, historyKeymap, undo, redo } from '@codemirror/commands';
export { syntaxTree, syntaxHighlighting, HighlightStyle } from '@codemirror/language';
export { markdown, markdownLanguage } from '@codemirror/lang-markdown';
export { search, searchKeymap, highlightSelectionMatches } from '@codemirror/search';
export { tags } from '@lezer/highlight';
EOF
npx esbuild entry.js --bundle --minify --format=iife --global-name=CM --legal-comments=none \
  --banner:js="/* CodeMirror 6 (MIT), bundled by scripts/build-codemirror.sh */" --outfile="$out"
echo "Wrote $out"
