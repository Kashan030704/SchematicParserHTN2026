# Three.js r128

Pinned to the incoming frontend's version, 0.128.0 (r128). Served locally so the
decorative background does not depend on a CDN or broaden the UI's `script-src 'self'`
policy. No npm/build step is needed. The robot-control script is separate; missing
WebGL falls back to a plain background, and reduced-motion mode uses a static frame.

Upstream files, retained under the accompanying MIT license:

- https://github.com/mrdoob/three.js/blob/r128/build/three.min.js
- https://github.com/mrdoob/three.js/blob/r128/LICENSE
