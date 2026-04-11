#!/bin/bash
# Run from ~/arbiter: bash sync-palette.sh
# Syncs dashboard.html and demo.html to the Protoss blue palette

set -e
cd "$(dirname "$0")" 2>/dev/null || true

DASH="public/dashboard.html"
DEMO="public/demo.html"

echo "Patching $DASH..."

# Fix .io -> .to
sed -i '' 's/ARBITER<span>\.io<\/span>/ARBITER<span>.to<\/span>/g' "$DASH"

# Background / surface colors
sed -i '' 's/--bg: #0a0c0f/--bg: #010409/g' "$DASH"
sed -i '' 's/--surface: #111318/--surface: #04080f/g' "$DASH"
sed -i '' 's/--surface2: #181c23/--surface2: #060c18/g' "$DASH"

# Accent: yellow -> blue
sed -i '' 's/--accent: #e8f060/--accent: #4a9eff/g' "$DASH"
sed -i '' 's/color: var(--accent)/color: var(--accent)/g' "$DASH"
# Replace hex yellow instances (logo, borders, highlights)
sed -i '' 's/#e8f060/#4a9eff/g' "$DASH"

# Critical / ok palette alignment
sed -i '' 's/--critical: #ff5c5c/--critical: #ff4466/g' "$DASH"
sed -i '' 's/#ff5c5c/#ff4466/g' "$DASH"
sed -i '' 's/--ok: #4adf96/--ok: #00e5a0/g' "$DASH"
sed -i '' 's/#4adf96/#00e5a0/g' "$DASH"

# Border colors -> blue-tinted
sed -i '' 's/--border: rgba(255,255,255,0.07)/--border: rgba(0,180,255,0.08)/g' "$DASH"
sed -i '' 's/--border2: rgba(255,255,255,0.12)/--border2: rgba(0,180,255,0.15)/g' "$DASH"
sed -i '' 's/rgba(255,255,255,0.07)/rgba(0,180,255,0.08)/g' "$DASH"
sed -i '' 's/rgba(255,255,255,0.12)/rgba(0,180,255,0.15)/g' "$DASH"

# Text / muted colors
sed -i '' 's/--muted: #7a8194/--muted: #2a4a6a/g' "$DASH"
sed -i '' 's/#7a8194/#2a4a6a/g' "$DASH"

# Nav: add Scale link after Demo link if not already present
if ! grep -q '/#scale' "$DASH"; then
  sed -i '' 's|href="/demo" .*class="nav-link">Demo</a>|href="/demo" class="nav-link">Demo</a><a href="/#scale" class="nav-link">Scale</a>|g' "$DASH"
fi

echo "  done."

echo "Patching $DEMO..."

# Fix .io -> .to (safety check)
sed -i '' 's/ARBITER<span>\.io<\/span>/ARBITER<span>.to<\/span>/g' "$DEMO"

# Add Scale nav link if not present
if ! grep -q '/#scale' "$DEMO"; then
  sed -i '' 's|<a href="/demo" class="nav-link active">Demo</a>|<a href="/demo" class="nav-link active">Demo</a><a href="/#scale" class="nav-link">Scale</a>|g' "$DEMO"
fi

echo "  done."

echo ""
echo "All patches applied. Now commit and deploy:"
echo "  git add public/dashboard.html public/demo.html"
echo "  git commit -m \"sync dashboard + demo to Protoss blue palette\""
echo "  git push origin main"
echo "  vercel --prod"
