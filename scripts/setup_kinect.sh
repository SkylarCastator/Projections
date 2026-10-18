#!/usr/bin/env bash
# Build libfreenect2 (Kinect v2 driver) into ~/.local/freenect2 and install the Python binding
# into this project's venv. No sudo here; the two sudo steps are printed at the end.
#
#   scripts/setup_kinect.sh            # build + install binding
#   scripts/setup_kinect.sh --check    # just report what is missing
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/../third_party/libfreenect2"
PREFIX="$HOME/.local/freenect2"
VENV="$HERE/.venv"

need_pkgs=(libusb-1.0-0-dev libturbojpeg0-dev libglfw3-dev)
missing=()
for p in "${need_pkgs[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done

kinect_speed=""
for d in /sys/bus/usb/devices/[0-9]-*; do
  if [ -f "$d/idProduct" ] && [ "$(cat "$d/idVendor")" = "045e" ] && grep -q -E "02c4|02d8|02d9" "$d/idProduct"; then
    kinect_speed="$(cat "$d/speed")"; break
  fi
done
echo "Kinect v2 link speed: ${kinect_speed:-not found}M (needs 5000M = USB 3 SuperSpeed)"
[ "${kinect_speed:-0}" -ge 5000 ] 2>/dev/null || echo "  -> replug into a USB 3 port / use the Kinect adapter's USB 3 cable; 480M will never stream"

if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing build packages: ${missing[*]}"
  echo "  run:  sudo apt install -y ${missing[*]}"
fi
if [ ! -f /etc/udev/rules.d/90-kinect2.rules ]; then
  echo "Missing udev rule (device access without root):"
  echo "  run:  sudo cp $SRC/platform/linux/udev/90-kinect2.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger"
fi
[ "${1:-}" = "--check" ] && exit 0
[ ${#missing[@]} -eq 0 ] || { echo "install the packages above first"; exit 1; }

[ -d "$SRC" ] || git clone --depth 1 https://github.com/OpenKinect/libfreenect2.git "$SRC"
mkdir -p "$SRC/build"
cd "$SRC/build"
# Intel iGPU, no CUDA: OpenGL pipeline for depth processing (needs GLFW); OpenCL off unless an ICD exists
OPENCL=OFF; [ -d /etc/OpenCL/vendors ] && [ -n "$(ls -A /etc/OpenCL/vendors 2>/dev/null)" ] && OPENCL=ON
cmake .. -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_BUILD_TYPE=Release \
  -DENABLE_CUDA=OFF -DENABLE_OPENCL=$OPENCL -DENABLE_OPENGL=ON -DENABLE_VAAPI=OFF -DENABLE_TEGRAJPEG=OFF \
  -DBUILD_OPENNI2_DRIVER=OFF -DBUILD_EXAMPLES=ON
make -j"$(nproc)"
make install
echo "libfreenect2 installed in $PREFIX (Protonect test tool: $PREFIX/bin/Protonect)"

# Python binding (cffi) against that install
export LIBFREENECT2_INSTALL_PREFIX="$PREFIX"
export C_INCLUDE_PATH="$PREFIX/include${C_INCLUDE_PATH:+:$C_INCLUDE_PATH}"
export CPLUS_INCLUDE_PATH="$PREFIX/include${CPLUS_INCLUDE_PATH:+:$CPLUS_INCLUDE_PATH}"
export LIBRARY_PATH="$PREFIX/lib${LIBRARY_PATH:+:$LIBRARY_PATH}"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export LDFLAGS="-Wl,-rpath,$PREFIX/lib${LDFLAGS:+ $LDFLAGS}"  # extension finds libfreenect2.so without LD_LIBRARY_PATH
uv pip install --python "$VENV/bin/python" --no-cache --reinstall-package freenect2 cffi freenect2
echo
echo "Done. promap finds the Kinect automatically (Camera dock -> Kinect)."
