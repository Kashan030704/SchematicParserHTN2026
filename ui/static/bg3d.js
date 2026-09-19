/*
 * Decorative background only. Renders a floating 3D circuit-board /
 * schematic layout behind the page content and drifts it through 3D
 * space as the user scrolls. Does not touch, read, or depend on any
 * app.js state — purely cosmetic and safe to remove without affecting
 * functionality.
 */
(function () {
  if (typeof THREE === 'undefined') return;
  const canvas = document.getElementById('bg-canvas');
  if (!canvas) return;

  const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(50, window.innerWidth / window.innerHeight, 0.1, 100);
  camera.position.set(0, 4.4, 8.5);
  camera.lookAt(0, 0, 0);

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.setClearColor(0xf3f5ef, 1);
  scene.fog = new THREE.Fog(0xf3f5ef, 9, 26);

  scene.add(new THREE.AmbientLight(0xffffff, 0.7));
  const key = new THREE.DirectionalLight(0xffffff, 0.9);
  key.position.set(4, 6, 6);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x9fd8c2, 0.55);
  rim.position.set(-5, -2, -4);
  scene.add(rim);

  const boardGroup = new THREE.Group();
  scene.add(boardGroup);

  // Silkscreen + solder-mask detail baked into a canvas texture, so the bare
  // green board reads as a real PCB (labels, grid dots, ref-des text) instead
  // of a flat slab.
  function boardSurfaceTexture() {
    const w = 1408, h = 832;
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#1f5c46';
    ctx.fillRect(0, 0, w, h);
    // subtle solder-mask sheen banding
    const sheen = ctx.createLinearGradient(0, 0, w, h);
    sheen.addColorStop(0, 'rgba(255,255,255,0.05)');
    sheen.addColorStop(0.5, 'rgba(255,255,255,0)');
    sheen.addColorStop(1, 'rgba(0,0,0,0.08)');
    ctx.fillStyle = sheen;
    ctx.fillRect(0, 0, w, h);
    // fabrication dot grid
    ctx.fillStyle = 'rgba(255,255,255,0.14)';
    for (let gx = 24; gx < w; gx += 48) {
      for (let gy = 24; gy < h; gy += 48) {
        ctx.beginPath();
        ctx.arc(gx, gy, 1.4, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    // silkscreen outlines + refdes labels
    ctx.strokeStyle = 'rgba(255,255,255,0.75)';
    ctx.fillStyle = 'rgba(255,255,255,0.75)';
    ctx.font = '20px monospace';
    ctx.lineWidth = 1.5;
    const labels = [
      { x: 120, y: 140, w: 90, h: 90, text: 'R1' },
      { x: 260, y: 620, w: 90, h: 90, text: 'R2' },
      { x: 980, y: 150, w: 100, h: 60, text: 'C1' },
      { x: 1040, y: 640, w: 100, h: 60, text: 'C2' },
      { x: 560, y: 120, w: 220, h: 90, text: 'U1' },
      { x: 560, y: 600, w: 220, h: 90, text: 'U2' },
    ];
    labels.forEach(({ x, y, w: bw, h: bh, text }) => {
      ctx.strokeRect(x - bw / 2, y - bh / 2, bw, bh);
      ctx.fillText(text, x - bw / 2, y - bh / 2 - 8);
    });
    // silkscreen title text
    ctx.font = 'bold 34px monospace';
    ctx.fillStyle = 'rgba(255,255,255,0.55)';
    ctx.fillText('SCHEMATIC-TO-FETCH · REV A', w / 2 - 260, h - 36);
    // polarity marks + a mini "logo" crest
    ctx.strokeStyle = 'rgba(255,255,255,0.5)';
    ctx.beginPath();
    ctx.arc(w / 2, h / 2, 46, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(w / 2 - 22, h / 2);
    ctx.lineTo(w / 2 + 22, h / 2);
    ctx.moveTo(w / 2, h / 2 - 22);
    ctx.lineTo(w / 2, h / 2 + 22);
    ctx.stroke();
    const tex = new THREE.CanvasTexture(c);
    tex.anisotropy = 4;
    tex.needsUpdate = true;
    return tex;
  }

  const board = new THREE.Mesh(
    new THREE.BoxGeometry(11, 0.12, 6.4),
    new THREE.MeshStandardMaterial({ map: boardSurfaceTexture(), roughness: 0.55, metalness: 0.15 })
  );
  boardGroup.add(board);

  // Plated mounting holes near each corner.
  [[-5.2, -2.8], [5.2, -2.8], [-5.2, 2.8], [5.2, 2.8]].forEach(([x, z]) => {
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(0.16, 0.03, 10, 20),
      new THREE.MeshStandardMaterial({ color: 0xcfcfcf, metalness: 0.85, roughness: 0.25 })
    );
    ring.rotation.x = Math.PI / 2;
    ring.position.set(x, 0.07, z);
    boardGroup.add(ring);
  });

  // Copper vias scattered across the board for that populated-PCB look.
  const viaMaterial = new THREE.MeshStandardMaterial({ color: 0xd8b25c, metalness: 0.75, roughness: 0.3 });
  for (let i = 0; i < 46; i++) {
    const via = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, 0.02, 8), viaMaterial);
    via.position.set((Math.random() - 0.5) * 10, 0.065, (Math.random() - 0.5) * 6);
    boardGroup.add(via);
  }

  function addTrace(x, z, length, rotationY) {
    const trace = new THREE.Mesh(
      new THREE.BoxGeometry(length, 0.015, 0.08),
      new THREE.MeshStandardMaterial({ color: 0xd8b25c, metalness: 0.7, roughness: 0.3 })
    );
    trace.position.set(x, 0.07, z);
    trace.rotation.y = rotationY;
    boardGroup.add(trace);
  }
  for (let i = -4; i <= 4; i++) {
    addTrace(i * 1.3, 2.2, 3.6, 0);
    addTrace(i * 1.3 + 0.5, -2.2, 2.8, Math.PI / 2);
  }
  // Finer secondary traces + right-angle "stair-step" routing for density.
  for (let i = -3; i <= 3; i++) {
    addTrace(i * 0.9, 0.3, 0.9, Math.PI / 2);
  }
  addTrace(-1.6, 1.0, 1.8, 0.12);
  addTrace(1.9, -0.6, 2.0, -0.1);
  addTrace(0, 0, 1.2, Math.PI / 2);

  // Pin-header connector along the back edge, a common real-PCB detail.
  const headerGroup = new THREE.Group();
  const headerBase = new THREE.Mesh(
    new THREE.BoxGeometry(2.4, 0.16, 0.3),
    new THREE.MeshStandardMaterial({ color: 0x14161a, roughness: 0.5 })
  );
  headerGroup.add(headerBase);
  for (let i = 0; i < 8; i++) {
    const pin = new THREE.Mesh(
      new THREE.BoxGeometry(0.05, 0.28, 0.05),
      new THREE.MeshStandardMaterial({ color: 0xc9c9c9, metalness: 0.85, roughness: 0.2 })
    );
    pin.position.set(-1.05 + i * 0.3, 0.08, 0);
    headerGroup.add(pin);
  }
  headerGroup.position.set(-3, 0.14, -2.9);
  boardGroup.add(headerGroup);

  function resistor(x, z, rotationY, bodyColor) {
    const group = new THREE.Group();
    const body = new THREE.Mesh(
      new THREE.CylinderGeometry(0.16, 0.16, 0.7, 24),
      new THREE.MeshStandardMaterial({ color: bodyColor || 0xdcb877, roughness: 0.5 })
    );
    body.rotation.z = Math.PI / 2;
    group.add(body);
    [0x1c1c1c, 0xb5342c, 0xd8b25c].forEach((color, i) => {
      const band = new THREE.Mesh(
        new THREE.CylinderGeometry(0.165, 0.165, 0.05, 24),
        new THREE.MeshStandardMaterial({ color })
      );
      band.rotation.z = Math.PI / 2;
      band.position.x = -0.18 + i * 0.18;
      group.add(band);
    });
    [-0.55, 0.55].forEach(leadX => {
      const lead = new THREE.Mesh(
        new THREE.CylinderGeometry(0.03, 0.03, 0.5, 8),
        new THREE.MeshStandardMaterial({ color: 0xb8b8b8, metalness: 0.8, roughness: 0.3 })
      );
      lead.rotation.z = Math.PI / 2;
      lead.position.x = leadX;
      group.add(lead);
    });
    group.position.set(x, 0.22, z);
    group.rotation.y = rotationY;
    boardGroup.add(group);
  }

  function capacitor(x, z) {
    const group = new THREE.Group();
    const body = new THREE.Mesh(
      new THREE.CylinderGeometry(0.22, 0.22, 0.5, 24),
      new THREE.MeshStandardMaterial({ color: 0x2b6cb0, roughness: 0.4 })
    );
    group.add(body);
    [-0.1, 0.1].forEach(legX => {
      const leg = new THREE.Mesh(
        new THREE.CylinderGeometry(0.02, 0.02, 0.35, 8),
        new THREE.MeshStandardMaterial({ color: 0xb8b8b8, metalness: 0.7 })
      );
      leg.position.set(legX, -0.42, 0);
      group.add(leg);
    });
    group.position.set(x, 0.31, z);
    boardGroup.add(group);
  }

  function chip(x, z) {
    const group = new THREE.Group();
    const body = new THREE.Mesh(
      new THREE.BoxGeometry(1.1, 0.22, 0.6),
      new THREE.MeshStandardMaterial({ color: 0x111318, roughness: 0.6 })
    );
    group.add(body);
    const notch = new THREE.Mesh(
      new THREE.CylinderGeometry(0.05, 0.05, 0.24, 12),
      new THREE.MeshStandardMaterial({ color: 0x2a2d33 })
    );
    notch.rotation.x = Math.PI / 2;
    notch.position.set(-0.5, 0.12, 0);
    group.add(notch);
    for (let i = -1; i <= 1; i++) {
      [-0.62, 0.62].forEach(side => {
        const leg = new THREE.Mesh(
          new THREE.BoxGeometry(0.18, 0.03, 0.04),
          new THREE.MeshStandardMaterial({ color: 0xc0c0c0, metalness: 0.7 })
        );
        leg.position.set(side, -0.02, i * 0.18);
        group.add(leg);
      });
    }
    group.position.set(x, 0.24, z);
    boardGroup.add(group);
  }

  // A small onboard LED (dome + flat-side marker + two leads) for realism.
  function led(x, z, color) {
    const group = new THREE.Group();
    const dome = new THREE.Mesh(
      new THREE.SphereGeometry(0.11, 16, 16, 0, Math.PI * 2, 0, Math.PI * 0.62),
      new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.6, transparent: true, opacity: 0.85, roughness: 0.25 })
    );
    group.add(dome);
    [-0.05, 0.05].forEach(legX => {
      const lg = new THREE.Mesh(
        new THREE.CylinderGeometry(0.015, 0.015, 0.22, 6),
        new THREE.MeshStandardMaterial({ color: 0xb0b0b0, metalness: 0.75 })
      );
      lg.position.set(legX, -0.14, 0);
      group.add(lg);
    });
    group.position.set(x, 0.18, z);
    boardGroup.add(group);
  }

  // A tiny slide switch, adding another distinct silhouette to the layout.
  function switchToggle(x, z) {
    const group = new THREE.Group();
    const base = new THREE.Mesh(
      new THREE.BoxGeometry(0.36, 0.12, 0.22),
      new THREE.MeshStandardMaterial({ color: 0x2b2f36, roughness: 0.55 })
    );
    group.add(base);
    const lever = new THREE.Mesh(
      new THREE.BoxGeometry(0.1, 0.05, 0.12),
      new THREE.MeshStandardMaterial({ color: 0xe4e4e4, roughness: 0.4 })
    );
    lever.position.set(0.06, 0.09, 0);
    group.add(lever);
    group.position.set(x, 0.2, z);
    boardGroup.add(group);
  }


  resistor(-3.8, -1.6, Math.PI / 2, 0xdcb877);
  resistor(0.6, -1.8, Math.PI / 2, 0xdcb877);
  resistor(4.1, 1.2, Math.PI / 6, 0xe0c9a0);
  resistor(4.3, -1.4, -Math.PI / 5, 0xdcb877);
  capacitor(-1.2, 2.1);
  capacitor(2.6, -0.9);
  capacitor(-2.6, 0.2);
  chip(-2.1, -1.1);
  chip(1.8, 1.9);
  led(3.4, 0.1, 0xe0554a);
  led(-4.6, -0.6, 0x4ac9e0);
  switchToggle(3.2, 2.4);

  boardGroup.rotation.x = -0.55;
  boardGroup.position.y = -0.6;

  // --- Ambient aesthetics around the board (not on it) -------------------

  // Soft radial-glow sprite texture, reused for bokeh dots and LED halos.
  function glowTexture(hexColor) {
    const size = 128;
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const ctx = c.getContext('2d');
    const grad = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    grad.addColorStop(0, hexColor);
    grad.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, size, size);
    const tex = new THREE.CanvasTexture(c);
    tex.needsUpdate = true;
    return tex;
  }

  // Drifting bokeh/solder-spark particles floating around the board at
  // varying depths — purely atmospheric, gives the scene warmth and parallax.
  const particleColors = ['rgba(216,178,92,0.9)', 'rgba(159,216,194,0.85)', 'rgba(255,255,255,0.8)'];
  const PARTICLE_COUNT = 70;
  const particles = [];
  const particleGroup = new THREE.Group();
  scene.add(particleGroup);
  for (let i = 0; i < PARTICLE_COUNT; i++) {
    const color = particleColors[i % particleColors.length];
    const scale = 0.12 + Math.random() * 0.3;
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTexture(color), transparent: true, depthWrite: false, opacity: 0.55 + Math.random() * 0.35,
    }));
    sprite.scale.set(scale, scale, 1);
    const radius = 5 + Math.random() * 8;
    const angle = Math.random() * Math.PI * 2;
    sprite.position.set(Math.cos(angle) * radius, Math.random() * 6 - 1.5, Math.sin(angle) * radius - 2);
    particleGroup.add(sprite);
    particles.push({ sprite, speed: 0.05 + Math.random() * 0.1, phase: Math.random() * Math.PI * 2, driftX: (Math.random() - 0.5) * 0.15 });
  }

  // A handful of small satellite PCB chips orbiting farther back, giving the
  // scene depth/parallax without cluttering the main board.
  function miniChip(color) {
    const group = new THREE.Group();
    const body = new THREE.Mesh(
      new THREE.BoxGeometry(0.55, 0.1, 0.32),
      new THREE.MeshStandardMaterial({ color: 0x141414, roughness: 0.6 })
    );
    group.add(body);
    const led = new THREE.Mesh(
      new THREE.SphereGeometry(0.05, 12, 12),
      new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 1.4 })
    );
    led.position.set(0.18, 0.09, 0);
    group.add(led);
    group.userData.led = led;
    return group;
  }
  const satellites = [];
  const satelliteColors = [0xe0554a, 0x4ac9e0, 0xd8b25c];
  satelliteColors.forEach((color, i) => {
    const sat = miniChip(color);
    const radius = 6.5 + i * 1.4;
    sat.userData.radius = radius;
    sat.userData.speed = 0.08 + i * 0.03;
    sat.userData.phase = (i / satelliteColors.length) * Math.PI * 2;
    sat.userData.height = 1.5 - i * 0.6;
    scene.add(sat);
    satellites.push(sat);
  });

  // Faint reference grid floor beneath everything, suggesting a lab bench.
  const grid = new THREE.GridHelper(40, 40, 0xb9c9b4, 0xdbe4d6);
  grid.position.y = -3.4;
  grid.material.transparent = true;
  grid.material.opacity = 0.35;
  scene.add(grid);

  let targetScroll = 0;
  let currentScroll = 0;

  function onScroll() {
    const doc = document.documentElement;
    const max = Math.max(doc.scrollHeight - window.innerHeight, 1);
    targetScroll = window.scrollY / max;
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  function onResize() {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  }
  window.addEventListener('resize', onResize);

  const clock = new THREE.Clock();
  function animate() {
    requestAnimationFrame(animate);
    currentScroll += (targetScroll - currentScroll) * 0.06;
    const t = reduceMotion ? 0 : clock.getElapsedTime();

    boardGroup.rotation.y = currentScroll * Math.PI * 2 + t * 0.04;
    boardGroup.rotation.x = -0.55 + Math.sin(currentScroll * Math.PI) * 0.25;
    boardGroup.position.z = -currentScroll * 6;
    boardGroup.position.y = -0.6 + currentScroll * 1.6;
    camera.position.x = Math.sin(currentScroll * Math.PI * 2) * 1.6;
    camera.lookAt(0, 0, 0);

    if (!reduceMotion) {
      particles.forEach(p => {
        p.sprite.position.y += p.speed * 0.02;
        p.sprite.position.x += Math.sin(t * 0.6 + p.phase) * p.driftX * 0.02;
        if (p.sprite.position.y > 6.5) p.sprite.position.y = -3;
      });
      satellites.forEach(sat => {
        const angle = t * sat.userData.speed + sat.userData.phase;
        sat.position.set(
          Math.cos(angle) * sat.userData.radius,
          sat.userData.height + Math.sin(t * 0.5 + sat.userData.phase) * 0.4,
          Math.sin(angle) * sat.userData.radius - 4
        );
        sat.rotation.y = -angle + Math.PI / 2;
        const pulse = 0.9 + Math.sin(t * 2.2 + sat.userData.phase) * 0.6;
        sat.userData.led.material.emissiveIntensity = Math.max(0.3, pulse);
      });
    }

    renderer.render(scene, camera);
  }
  animate();
})();
