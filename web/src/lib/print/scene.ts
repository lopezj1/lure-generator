/**
 * three.js scene for the print preview: one indexed LineSegments over the toolpath polyline,
 * colored in the shader by feature or tool, with a feature-visibility mask in uniforms.
 * Renders on demand only (camera moves, slider changes, resizes).
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import type { PrintToolpath } from '../toolpath/reader';
import { buildPrintBuffers } from './model';
import { BACKGROUND, PAUSE_COLOR, TRAVEL_COLOR, featureColor, rgb, toolColor } from './palette';

const MAX_FEATURES = 64;
const MAX_TOOLS = 16;

const VERTEX = /* glsl */ `
attribute vec4 aSeg; // kind, feature, tool, unused
uniform vec4 uFeature[${MAX_FEATURES}]; // rgb, visible
uniform vec3 uTool[${MAX_TOOLS}];
uniform vec3 uTravel;
uniform float uShowTravel;
uniform float uByTool;
uniform float uDimBelow;
flat varying vec4 vColor;
void main() {
  int kind = int(aSeg.x + 0.5);
  vec4 feature = uFeature[min(int(aSeg.y + 0.5), ${MAX_FEATURES - 1})];
  vec3 color;
  float keep;
  if (kind == 1) {
    color = uTravel;
    keep = uShowTravel;
  } else {
    color = uByTool > 0.5 ? uTool[min(int(aSeg.z + 0.5), ${MAX_TOOLS - 1})] : feature.rgb;
    keep = kind == 0 ? 0.0 : feature.a;
    if (position.z < uDimBelow) color *= 0.55;
  }
  vColor = vec4(color, keep);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

const FRAGMENT = /* glsl */ `
flat varying vec4 vColor;
void main() {
  if (vColor.a < 0.5) discard;
  gl_FragColor = vec4(vColor.rgb, 1.0);
}
`;

export type ColorMode = 'feature' | 'tool';

export class PrintScene {
  readonly renderer: THREE.WebGLRenderer;
  private readonly scene = new THREE.Scene();
  private readonly camera = new THREE.PerspectiveCamera(40, 1, 0.5, 10000);
  private readonly controls: OrbitControls;
  private readonly resize: ResizeObserver;
  private readonly material: THREE.ShaderMaterial;
  private readonly nozzle: THREE.Mesh;
  private readonly decor = new THREE.Group();
  private lines: THREE.LineSegments | null = null;
  private tp: PrintToolpath | null = null;
  private pending = false;
  /** Frames drawn and the CPU time of the last one (for the perf readout). */
  frames = 0;
  lastFrameMs = 0;

  constructor(private readonly container: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setClearColor(BACKGROUND);
    container.appendChild(this.renderer.domElement);

    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.screenSpacePanning = true;
    this.controls.addEventListener('change', () => this.requestRender());

    this.material = new THREE.ShaderMaterial({
      vertexShader: VERTEX,
      fragmentShader: FRAGMENT,
      uniforms: {
        uFeature: { value: new Float32Array(4 * MAX_FEATURES) },
        uTool: { value: new Float32Array(3 * MAX_TOOLS) },
        uTravel: { value: new THREE.Vector3(...rgb(TRAVEL_COLOR)) },
        uShowTravel: { value: 0 },
        uByTool: { value: 0 },
        uDimBelow: { value: -1e9 },
      },
    });

    const cone = new THREE.ConeGeometry(1.6, 5, 20);
    cone.rotateX(-Math.PI / 2); // tip toward −z
    cone.translate(0, 0, 2.5); // tip at the origin
    this.nozzle = new THREE.Mesh(
      cone,
      new THREE.MeshLambertMaterial({ color: '#e8e8e8', emissive: '#404040' }),
    );
    this.nozzle.visible = false;
    this.scene.add(this.nozzle, this.decor);
    this.scene.add(new THREE.HemisphereLight('#ffffff', '#606060', 2.5));

    this.resize = new ResizeObserver(() => this.onResize());
    this.resize.observe(container);
    this.onResize();
  }

  load(tp: PrintToolpath): void {
    this.clear();
    this.tp = tp;
    const { index, attrs } = buildPrintBuffers(tp);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(tp.points, 3));
    geometry.setAttribute('aSeg', new THREE.BufferAttribute(attrs, 4));
    geometry.setIndex(new THREE.BufferAttribute(index, 1));
    const b = tp.header.bounds;
    if (b) {
      const box = new THREE.Box3(new THREE.Vector3(...b.min), new THREE.Vector3(...b.max));
      geometry.boundingBox = box;
      geometry.boundingSphere = box.getBoundingSphere(new THREE.Sphere());
    }
    this.lines = new THREE.LineSegments(geometry, this.material);
    this.scene.add(this.lines);

    const features = tp.header.features ?? [];
    const f = this.material.uniforms.uFeature.value as Float32Array;
    features.slice(0, MAX_FEATURES).forEach((name, i) => {
      f.set([...rgb(featureColor(name)), 1], 4 * i);
    });
    const t = this.material.uniforms.uTool.value as Float32Array;
    tp.header.tools.slice(0, MAX_TOOLS).forEach((tool, i) => t.set(toolColor(tool.color), 3 * i));

    this.addDecor(tp);
    this.nozzle.visible = true;
    this.fit();
  }

  private addDecor(tp: PrintToolpath): void {
    const area = tp.header.bounds_active ?? tp.header.bounds;
    if (!area) return;
    const [x0, y0] = area.min;
    const [x1, y1] = area.max;
    const cx = (x0 + x1) / 2;
    const cy = (y0 + y1) / 2;
    // Bed grid at z = 0 on a 10 mm pitch around the part.
    const size = Math.ceil((Math.max(x1 - x0, y1 - y0) + 40) / 10) * 10;
    const grid = new THREE.GridHelper(size, size / 10, '#596270', '#3a3f48');
    grid.rotateX(Math.PI / 2);
    grid.position.set(Math.round(cx / 10) * 10, Math.round(cy / 10) * 10, 0);
    this.decor.add(grid);
    // Pause planes (translucent) at the pause Z.
    for (const m of tp.header.markers) {
      if (m.type !== 'pause') continue;
      const z = tp.header.layers?.[m.layer ?? 0]?.z ?? tp.points[3 * m.segment + 2];
      const plane = new THREE.Mesh(
        new THREE.PlaneGeometry(x1 - x0 + 20, y1 - y0 + 20),
        new THREE.MeshBasicMaterial({
          color: PAUSE_COLOR,
          transparent: true,
          opacity: 0.15,
          side: THREE.DoubleSide,
          depthWrite: false,
        }),
      );
      plane.position.set(cx, cy, z);
      plane.renderOrder = 1;
      plane.name = `pause:${m.line}`;
      this.decor.add(plane);
    }
  }

  /** Draw segments [start, end) and park the nozzle at the end of the last one. */
  setRange(start: number, end: number): void {
    if (!this.lines || !this.tp) return;
    this.lines.geometry.setDrawRange(2 * start, 2 * Math.max(0, end - start));
    const p = this.tp.points;
    const i = Math.max(start, end);
    if (3 * i + 2 < p.length) this.nozzle.position.set(p[3 * i], p[3 * i + 1], p[3 * i + 2]);
    this.requestRender();
  }

  setColorMode(mode: ColorMode): void {
    this.material.uniforms.uByTool.value = mode === 'tool' ? 1 : 0;
    this.requestRender();
  }

  setFeatureVisible(feature: number, visible: boolean): void {
    if (feature < 0 || feature >= MAX_FEATURES) return;
    (this.material.uniforms.uFeature.value as Float32Array)[4 * feature + 3] = visible ? 1 : 0;
    this.requestRender();
  }

  setShowTravel(show: boolean): void {
    this.material.uniforms.uShowTravel.value = show ? 1 : 0;
    this.requestRender();
  }

  /** Dim extrusions below `z` (the top layer while scrubbing); null turns it off. */
  setDimBelow(z: number | null): void {
    this.material.uniforms.uDimBelow.value = z ?? -1e9;
    this.requestRender();
  }

  setPausePlanesVisible(visible: boolean): void {
    for (const o of this.decor.children) if (o.name.startsWith('pause:')) o.visible = visible;
    this.requestRender();
  }

  /** Frame the extruded part from the front-right, above. */
  fit(): void {
    const b = this.tp?.header.bounds_active ?? this.tp?.header.bounds;
    if (!b) return;
    const box = new THREE.Box3(new THREE.Vector3(...b.min), new THREE.Vector3(...b.max));
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const r = Math.max(sphere.radius, 5);
    const fov = THREE.MathUtils.degToRad(this.camera.fov);
    const dist = (r / Math.sin(fov / 2)) * (this.camera.aspect < 1 ? 1 / this.camera.aspect : 1);
    const dir = new THREE.Vector3(0.55, -1, 0.85).normalize();
    this.camera.position.copy(sphere.center).addScaledVector(dir, dist * 1.05);
    this.camera.near = Math.max(0.1, dist / 1000);
    this.camera.far = dist * 20;
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(sphere.center);
    this.controls.update();
    this.nozzle.scale.setScalar(Math.max(0.5, r / 60));
    this.requestRender();
  }

  requestRender(): void {
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => {
      this.pending = false;
      const t0 = performance.now();
      this.renderer.render(this.scene, this.camera);
      this.lastFrameMs = performance.now() - t0;
      this.frames++;
    });
  }

  private onResize(): void {
    const { clientWidth: w, clientHeight: h } = this.container;
    if (!w || !h) return;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.requestRender();
  }

  private clear(): void {
    if (this.lines) {
      this.scene.remove(this.lines);
      this.lines.geometry.dispose();
      this.lines = null;
    }
    for (const o of [...this.decor.children]) {
      this.decor.remove(o);
      if (o instanceof THREE.Mesh || o instanceof THREE.LineSegments) {
        o.geometry.dispose();
        const mats = Array.isArray(o.material) ? o.material : [o.material];
        mats.forEach((m: THREE.Material) => m.dispose());
      }
    }
    (this.material.uniforms.uFeature.value as Float32Array).fill(0);
    this.tp = null;
    this.nozzle.visible = false;
  }

  dispose(): void {
    this.clear();
    this.resize.disconnect();
    this.controls.dispose();
    this.material.dispose();
    this.nozzle.geometry.dispose();
    (this.nozzle.material as THREE.Material).dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
