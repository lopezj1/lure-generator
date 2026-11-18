/**
 * three.js scene for the CNC preview: one indexed LineSegments per file over its toolpath
 * polyline, colored by tool in the shader. A job-time uniform splits every move into done
 * (full color) and remaining (dimmed or hidden), down to the fragment, so scrubbing only
 * changes a uniform. Rapids are dashed and paler. Stock box, cut bounding box with
 * dimensions, work zero axes and a tool marker sit around it. Renders on demand only.
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { CSS2DObject, CSS2DRenderer } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import type { Bounds, Vec3 } from '../toolpath/reader';
import { BACKGROUND, rgb } from '../print/palette';
import { buildPartBuffers, type CncJob } from './model';
import { BBOX_COLOR, STOCK_COLOR, cncToolColor } from './palette';

const MAX_TOOLS = 32;

const VERTEX = /* glsl */ `
attribute vec4 aSeg;  // kind, job tool, 0, 0
attribute float aDist;
attribute vec4 aTime; // start, end, length of the segment, 0
uniform vec4 uTool[${MAX_TOOLS}]; // rgb, visible
uniform float uShowRapid;
flat varying vec4 vColor; // rgb, kind (-1 = hidden)
flat varying vec4 vSeg;   // start, end, length, distance at the segment start
varying float vDist;
void main() {
  int kind = int(aSeg.x + 0.5);
  vec4 tool = uTool[min(int(aSeg.y + 0.5), ${MAX_TOOLS - 1})];
  float keep = kind == 0 ? 0.0 : tool.a;
  if (kind == 1) keep *= uShowRapid;
  vColor = vec4(tool.rgb, keep > 0.5 ? float(kind) : -1.0);
  vSeg = vec4(aTime.xyz, aDist);
  vDist = aDist;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

const FRAGMENT = /* glsl */ `
uniform float uTime;
uniform float uRemaining; // 1 = dim, 0 = hide
uniform vec3 uBg;
uniform float uDash;
flat varying vec4 vColor;
flat varying vec4 vSeg;
varying float vDist;
void main() {
  if (vColor.a < 0.5) discard;
  vec3 c = vColor.rgb;
  if (vColor.a < 1.5) { // rapid: dashed and paler
    if (fract(vDist / uDash) > 0.5) discard;
    c = mix(c, vec3(0.85), 0.45);
  }
  float f = vSeg.z > 0.0 ? clamp((vDist - vSeg.w) / vSeg.z, 0.0, 1.0) : 1.0;
  float t = mix(vSeg.x, vSeg.y, f);
  if (t > uTime) {
    if (uRemaining < 0.5) discard;
    c = mix(uBg, c, 0.28);
  }
  gl_FragColor = vec4(c, 1.0);
}
`;

export type ViewPreset = 'top' | 'front' | 'side' | 'iso';

const VIEW_DIRS: Record<ViewPreset, Vec3> = {
  top: [0, -1e-4, 1],
  front: [0, -1, 0],
  side: [1, 0, 0],
  iso: [1, -1, 0.9],
};

export class CncScene {
  readonly renderer: THREE.WebGLRenderer;
  private readonly labels = new CSS2DRenderer();
  private readonly scene = new THREE.Scene();
  private readonly camera = new THREE.PerspectiveCamera(35, 1, 0.5, 20000);
  private readonly controls: OrbitControls;
  private readonly resize: ResizeObserver;
  private readonly material: THREE.ShaderMaterial;
  private readonly tool: THREE.Group;
  private readonly toolMat: THREE.MeshLambertMaterial;
  private readonly decor = new THREE.Group();
  private readonly lines: THREE.LineSegments[] = [];
  private job: CncJob | null = null;
  private view: ViewPreset = 'iso';
  /** Smallest marker diameter that stays visible when zoomed to the whole job. */
  private markerMin = 1;
  private pending = false;
  frames = 0;
  lastFrameMs = 0;

  constructor(private readonly container: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setClearColor(BACKGROUND);
    container.appendChild(this.renderer.domElement);
    const el = this.labels.domElement;
    el.style.position = 'absolute';
    el.style.inset = '0';
    el.style.pointerEvents = 'none';
    container.appendChild(el);

    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.screenSpacePanning = true;
    this.controls.addEventListener('change', () => this.requestRender());

    this.material = new THREE.ShaderMaterial({
      vertexShader: VERTEX,
      fragmentShader: FRAGMENT,
      uniforms: {
        uTool: { value: new Float32Array(4 * MAX_TOOLS) },
        uShowRapid: { value: 1 },
        uTime: { value: 0 },
        uRemaining: { value: 1 },
        uBg: { value: new THREE.Vector3(...rgb(BACKGROUND)) },
        uDash: { value: 2 },
      },
    });

    // Tool marker: a unit-diameter shank whose tip sits at the origin, scaled per tool.
    this.toolMat = new THREE.MeshLambertMaterial({
      color: '#d0d4da',
      emissive: '#303030',
      transparent: true,
      opacity: 0.85,
    });
    const shank = new THREE.CylinderGeometry(0.5, 0.5, 1, 24);
    shank.rotateX(Math.PI / 2);
    shank.translate(0, 0, 0.5);
    this.tool = new THREE.Group();
    this.tool.add(new THREE.Mesh(shank, this.toolMat));
    this.tool.visible = false;
    this.tool.renderOrder = 2;

    this.scene.add(this.tool, this.decor);
    this.scene.add(new THREE.HemisphereLight('#ffffff', '#606060', 2.5));

    this.resize = new ResizeObserver(() => this.onResize());
    this.resize.observe(container);
    this.onResize();
  }

  load(job: CncJob): void {
    this.clear();
    this.job = job;
    for (const part of job.parts) {
      if (!part.tp.segments) continue;
      const { index, attrs, dist, seg } = buildPartBuffers(part);
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(part.tp.points, 3));
      g.setAttribute('aSeg', new THREE.BufferAttribute(attrs, 4));
      g.setAttribute('aDist', new THREE.BufferAttribute(dist, 1));
      g.setAttribute('aTime', new THREE.BufferAttribute(seg, 4));
      g.setIndex(new THREE.BufferAttribute(index, 1));
      const b = part.tp.header.bounds;
      if (b) {
        const box = new THREE.Box3(new THREE.Vector3(...b.min), new THREE.Vector3(...b.max));
        g.boundingBox = box;
        g.boundingSphere = box.getBoundingSphere(new THREE.Sphere());
      }
      const lines = new THREE.LineSegments(g, this.material);
      this.lines.push(lines);
      this.scene.add(lines);
    }
    const t = this.material.uniforms.uTool.value as Float32Array;
    job.tools.slice(0, MAX_TOOLS).forEach((_, i) => t.set([...rgb(cncToolColor(i)), 1], 4 * i));
    const area = job.active ?? job.bounds;
    if (area) {
      const diag = new THREE.Vector3(...area.max).distanceTo(new THREE.Vector3(...area.min));
      this.material.uniforms.uDash.value = Math.max(0.5, diag / 200);
      this.markerMin = diag / 120;
    }
    this.addDecor(job);
    this.material.uniforms.uTime.value = job.duration;
    this.setView(this.view);
  }

  private addDecor(job: CncJob): void {
    if (job.stock) this.decor.add(box(job.stock, STOCK_COLOR, 'stock'));
    const cut = job.active ?? job.bounds;
    if (cut) {
      this.decor.add(box(cut, BBOX_COLOR, 'bbox'));
      const [x0, y0, z0] = cut.min;
      const [x1, y1, z1] = cut.max;
      const dims: [string, Vec3][] = [
        [`X ${(x1 - x0).toFixed(1)}`, [(x0 + x1) / 2, y0, z0]],
        [`Y ${(y1 - y0).toFixed(1)}`, [x1, (y0 + y1) / 2, z0]],
        [`Z ${(z1 - z0).toFixed(1)}`, [x1, y0, (z0 + z1) / 2]],
      ];
      for (const [text, at] of dims) this.decor.add(label(text, at, 'dim'));
    }
    const ref = job.stock ?? cut;
    const size = ref
      ? Math.max(5, 0.12 * Math.max(ref.max[0] - ref.min[0], ref.max[1] - ref.min[1]))
      : 10;
    const axes = new THREE.AxesHelper(size);
    axes.name = 'zero';
    this.decor.add(axes);
    this.decor.add(label('X0 Y0 Z0', [0, 0, 0], 'zero'));
  }

  setTime(t: number): void {
    this.material.uniforms.uTime.value = t;
    this.requestRender();
  }

  /**
   * Tool tip at `pos`; the marker has the tool's diameter (default 1/8"), enlarged on big
   * jobs so a fine end mill stays visible.
   */
  setToolMarker(pos: Vec3 | null, color: string, diameter: number | undefined): void {
    this.tool.visible = pos != null;
    if (pos) {
      const d = Math.max(diameter && diameter > 0 ? diameter : 3.175, this.markerMin);
      this.tool.position.set(...pos);
      this.tool.scale.set(d, d, Math.max(d * 6, 15));
      this.toolMat.color.set(color);
    }
    this.requestRender();
  }

  setShowRapids(show: boolean): void {
    this.material.uniforms.uShowRapid.value = show ? 1 : 0;
    this.requestRender();
  }

  setRemaining(mode: 'dim' | 'hide'): void {
    this.material.uniforms.uRemaining.value = mode === 'dim' ? 1 : 0;
    this.requestRender();
  }

  setToolVisible(tool: number, visible: boolean): void {
    if (tool < 0 || tool >= MAX_TOOLS) return;
    (this.material.uniforms.uTool.value as Float32Array)[4 * tool + 3] = visible ? 1 : 0;
    this.requestRender();
  }

  setDecorVisible(kind: 'stock' | 'bbox' | 'zero', visible: boolean): void {
    for (const o of this.decor.children) {
      if (o.name === kind || (kind === 'bbox' && o.name === 'dim')) o.visible = visible;
    }
    this.requestRender();
  }

  /** Frame stock and cuts from a preset direction. */
  setView(view: ViewPreset): void {
    this.view = view;
    const job = this.job;
    const b = job ? (unionBounds(job.stock, job.active ?? job.bounds) ?? job.bounds) : null;
    if (!b) return;
    const box = new THREE.Box3(new THREE.Vector3(...b.min), new THREE.Vector3(...b.max));
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const r = Math.max(sphere.radius, 5);
    const fov = THREE.MathUtils.degToRad(this.camera.fov);
    const dist = (r / Math.sin(fov / 2)) * (this.camera.aspect < 1 ? 1 / this.camera.aspect : 1);
    const dir = new THREE.Vector3(...VIEW_DIRS[view]).normalize();
    this.camera.position.copy(sphere.center).addScaledVector(dir, dist * 1.02);
    this.camera.near = Math.max(0.1, dist / 1000);
    this.camera.far = dist * 20;
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(sphere.center);
    this.controls.update();
    this.requestRender();
  }

  requestRender(): void {
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => {
      this.pending = false;
      const t0 = performance.now();
      this.renderer.render(this.scene, this.camera);
      this.labels.render(this.scene, this.camera);
      this.lastFrameMs = performance.now() - t0;
      this.frames++;
    });
  }

  private onResize(): void {
    const { clientWidth: w, clientHeight: h } = this.container;
    if (!w || !h) return;
    this.renderer.setSize(w, h);
    this.labels.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.requestRender();
  }

  private clear(): void {
    for (const l of this.lines) {
      this.scene.remove(l);
      l.geometry.dispose();
    }
    this.lines.length = 0;
    for (const o of [...this.decor.children]) {
      this.decor.remove(o);
      if (o instanceof CSS2DObject) o.element.remove();
      if (o instanceof THREE.LineSegments) {
        o.geometry.dispose();
        (o.material as THREE.Material).dispose();
      }
    }
    (this.material.uniforms.uTool.value as Float32Array).fill(0);
    this.job = null;
    this.tool.visible = false;
  }

  dispose(): void {
    this.clear();
    this.resize.disconnect();
    this.controls.dispose();
    this.material.dispose();
    for (const m of this.tool.children) {
      if (m instanceof THREE.Mesh) m.geometry.dispose();
    }
    this.toolMat.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
    this.labels.domElement.remove();
  }
}

function unionBounds(a: Bounds | null, b: Bounds | null): Bounds | null {
  if (!a) return b;
  if (!b) return a;
  return {
    min: a.min.map((v, k) => Math.min(v, b.min[k])) as Vec3,
    max: a.max.map((v, k) => Math.max(v, b.max[k])) as Vec3,
  };
}

function box(b: Bounds, color: string, name: string): THREE.LineSegments {
  const size = b.max.map((v, k) => Math.max(v - b.min[k], 1e-3)) as Vec3;
  const solid = new THREE.BoxGeometry(...size);
  const g = new THREE.EdgesGeometry(solid);
  solid.dispose();
  g.translate(...(b.min.map((v, k) => v + size[k] / 2) as Vec3));
  const lines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color }));
  lines.name = name;
  return lines;
}

function label(text: string, at: Vec3, name: string): CSS2DObject {
  const el = document.createElement('span');
  el.textContent = text;
  el.className = `cnc-label cnc-label-${name}`;
  const obj = new CSS2DObject(el);
  obj.position.set(...at);
  obj.name = name;
  return obj;
}
