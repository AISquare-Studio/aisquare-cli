/**
 * Spatialised alert audio.
 *
 * §8: "The alert cue is positioned in 3D at the panel's location so the
 * operator turns toward it. This is the one thing this medium does that a
 * monitor cannot — spend effort here."
 *
 * So the chime is not a notification sound played at the operator; it is a
 * sound emitted BY a panel, at that panel's position in the room, and it stays
 * there while the operator's head moves. Turning toward it is the point, and it
 * is the only channel in the client that can tell you a panel behind you needs
 * something.
 *
 * Synthesised, never loaded: an asset is one more fetch that can fail on a
 * headset behind `adb reverse`, for about 300 ms of sound.
 *
 * Autoplay: a Quest will not let an AudioContext start outside a user gesture,
 * and a context created before one is permanently "suspended" on some builds.
 * So nothing is constructed until `unlock()` is called from a real click or
 * keypress — the Enter AR button, or the first key a desktop tester presses.
 */

/** Roughly a small struck bell: a fundamental plus one inharmonic partial. */
const CHIME = {
  hz: 880,
  partial: 2.4, // inharmonic on purpose — a harmonic partial reads as a chord
  partialGain: 0.35,
  attackS: 0.006,
  decayS: 0.38,
  peak: 0.5,
};

export class AlertAudio {
  constructor() {
    this.ctx = null;
    this.muted = false;
    /** Reported through `window.__xr` so a tester can see why it is silent. */
    this.state = 'locked';
  }

  /**
   * Create or resume the context. MUST be called from inside a user gesture.
   * Safe to call repeatedly — that is the point, since the first gesture may
   * be Enter AR on one run and a keypress on another.
   */
  unlock() {
    const Ctor = globalThis.AudioContext ?? globalThis.webkitAudioContext;
    if (!Ctor) {
      this.state = 'unsupported';
      return null;
    }
    if (!this.ctx) {
      try {
        this.ctx = new Ctor();
      } catch (err) {
        this.state = 'unsupported';
        console.warn('[xr] Web Audio unavailable; alerts will be silent', err);
        return null;
      }
    }
    if (this.ctx.state === 'suspended') {
      this.ctx.resume().catch(() => {
        /* a second gesture will get it */
      });
    }
    this.state = this.ctx.state;
    return this.ctx;
  }

  /**
   * Move the listener onto the viewer. Called once per frame — and this is the
   * ONLY per-frame work in the client besides rendering and the alert pulse.
   *
   * @param {THREE.Vector3} position viewer world position
   * @param {THREE.Vector3} forward  unit world forward
   * @param {THREE.Vector3} up       unit world up
   */
  setListener(position, forward, up) {
    const listener = this.ctx?.listener;
    if (!listener) return;

    // The AudioParam form is the current spec; `setPosition`/`setOrientation`
    // are deprecated but are still the only form some builds implement.
    if (listener.positionX) {
      const t = this.ctx.currentTime;
      listener.positionX.setValueAtTime(position.x, t);
      listener.positionY.setValueAtTime(position.y, t);
      listener.positionZ.setValueAtTime(position.z, t);
      listener.forwardX.setValueAtTime(forward.x, t);
      listener.forwardY.setValueAtTime(forward.y, t);
      listener.forwardZ.setValueAtTime(forward.z, t);
      listener.upX.setValueAtTime(up.x, t);
      listener.upY.setValueAtTime(up.y, t);
      listener.upZ.setValueAtTime(up.z, t);
    } else if (listener.setPosition) {
      listener.setPosition(position.x, position.y, position.z);
      listener.setOrientation(forward.x, forward.y, forward.z, up.x, up.y, up.z);
    }
  }

  /**
   * Play the chime at a world position. Silently does nothing when the context
   * has not been unlocked yet, which is the correct behaviour for an alert that
   * arrives before the operator has touched anything.
   *
   * @param {{x:number,y:number,z:number}} at panel world position
   */
  chimeAt(at) {
    if (this.muted || !this.ctx || this.ctx.state !== 'running') return false;
    const ctx = this.ctx;
    const t = ctx.currentTime;

    const panner = ctx.createPanner();
    panner.panningModel = 'HRTF'; // the part that makes it a direction, not a side
    panner.distanceModel = 'inverse';
    panner.refDistance = 1;
    panner.maxDistance = 20;
    panner.rolloffFactor = 1;
    if (panner.positionX) {
      panner.positionX.setValueAtTime(at.x, t);
      panner.positionY.setValueAtTime(at.y, t);
      panner.positionZ.setValueAtTime(at.z, t);
    } else {
      panner.setPosition(at.x, at.y, at.z);
    }

    const gain = ctx.createGain();
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(CHIME.peak, t + CHIME.attackS);
    // Exponential, because a linear decay on a bell sounds like a fade-out.
    gain.gain.exponentialRampToValueAtTime(0.0001, t + CHIME.decayS);

    gain.connect(panner).connect(ctx.destination);

    const stopAt = t + CHIME.decayS + 0.02;
    for (const [hz, level] of [
      [CHIME.hz, 1],
      [CHIME.hz * CHIME.partial, CHIME.partialGain],
    ]) {
      const osc = ctx.createOscillator();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(hz, t);
      const trim = ctx.createGain();
      trim.gain.setValueAtTime(level, t);
      osc.connect(trim).connect(gain);
      osc.start(t);
      osc.stop(stopAt);
      // Nodes are single-use; without this the graph grows for the whole
      // session and every alert leaves a dead oscillator behind it.
      osc.onended = () => {
        osc.disconnect();
        trim.disconnect();
      };
    }
    setTimeout(
      () => {
        gain.disconnect();
        panner.disconnect();
      },
      (CHIME.decayS + 0.1) * 1000,
    );

    return true;
  }
}
