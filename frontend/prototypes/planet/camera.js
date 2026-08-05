// 相机状态机：overview（悬浮球远景）/ planet（全景近景）/ focus（话题聚焦）
// 状态切换 = 位置/朝向插值（手写缓动，运动质感：先快后慢 easeOutCubic）

import * as THREE from "three";

const STATES = {
  overview: { radius: 5.5, fov: 45 },
  planet: { radius: 2.6, fov: 45 },
  focus: { radius: 1.1, fov: 40 },
};

export class CameraRig {
  constructor(camera) {
    this.camera = camera;
    this.state = "overview";
    this._target = null;
    this._anim = null;
  }

  getState() {
    return this.state;
  }

  // 目标点（球面坐标）→ 相机位置：沿目标方向拉远 radius
  _positionFor(target, radius) {
    const dir = target.clone().normalize();
    return dir.multiplyScalar(radius);
  }

  async go(state, target = null, duration = 650) {
    if (target) this._target = target.clone();
    const conf = STATES[state];
    const endPos = this._positionFor(
      this._target || new THREE.Vector3(0, 0, 1),
      conf.radius
    );
    this.state = state;
    const startPos = this.camera.position.clone();
    const startFov = this.camera.fov;
    const t0 = performance.now();

    return new Promise((resolve) => {
      const step = (now) => {
        let t = (now - t0) / duration;
        if (t >= 1) t = 1;
        const e = 1 - Math.pow(1 - t, 3); // easeOutCubic
        this.camera.position.lerpVectors(startPos, endPos, e);
        this.camera.fov = startFov + (conf.fov - startFov) * e;
        this.camera.updateProjectionMatrix();
        if (this._target) this.camera.lookAt(this._target);
        if (t < 1) {
          this._anim = requestAnimationFrame(step);
        } else {
          resolve();
        }
      };
      this._anim = requestAnimationFrame(step);
    });
  }

  // 每帧调用：无动画时跟随目标（OrbitControls 拖动后重置朝向）
  update(delta) {
    if (!this._anim && this._target && this.state === "focus") {
      this.camera.lookAt(this._target);
    }
  }

  cancel() {
    if (this._anim) cancelAnimationFrame(this._anim);
    this._anim = null;
  }
}