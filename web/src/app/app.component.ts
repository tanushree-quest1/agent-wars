import { AfterViewInit, Component, NgZone, OnDestroy, OnInit, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from './api.service';

declare const L: any;

export interface PresetScenario {
  label: string;
  veh: string;
  tag: string;
  text: string;
}

const PRESETS: PresetScenario[] = [
  {
    label: 'Reefer failure + pallet damage',
    veh: 'TRK-3',
    tag: 'TRK-3 · Insulin 2-8C',
    text: 'Reefer unit has stopped cooling and the temperature display keeps climbing. Two pallets also got crushed when I braked hard. Truck is still drivable.',
  },
  {
    label: 'Compressor noise (telemetry check)',
    veh: 'TRK-4',
    tag: 'TRK-4 · Dairy 2-8C',
    text: 'Cooling compressor is making a loud noise, box temperature is rising, reefer failing, cargo dairy needs 2-8C.',
  },
  {
    label: 'Engine seized at toll plaza',
    veh: 'TRK-5',
    tag: 'TRK-5 · Medical supplies',
    text: "Engine seized near the toll plaza, truck won't start. Stuck on the highway.",
  },
  {
    label: 'Highway flooded ahead',
    veh: 'TRK-2',
    tag: 'TRK-2 · Appliances',
    text: 'Road ahead is flooded and police have blocked the highway, long jam.',
  },
  {
    label: 'Unverified driver report',
    veh: 'TRK-1',
    tag: 'TRK-1 · Low confidence',
    text: 'something is wrong with the truck, not sure what',
  },
];

const VEHICLE_COLORS: Record<string, string> = {
  in_transit: '#1d448f',
  available: '#196134',
  out_of_service: '#c43210',
  dispatched: '#8f5400',
};

const STATUS_CLASS: Record<string, string> = {
  executed: 'ok',
  actioned: 'ok',
  delivered: 'ok',
  available: 'ok',
  in_transit: 'blue',
  pending_approval: 'warn',
  awaiting_approval: 'warn',
  running: 'warn',
  recovering: 'warn',
  dispatched: 'warn',
  overridden: 'bad',
  needs_human: 'bad',
  rejected: 'bad',
  failed: 'bad',
  out_of_service: 'bad',
};

@Component({
  selector: 'app-root',
  imports: [FormsModule],
  templateUrl: './app.component.html',
})
export class AppComponent implements OnInit, AfterViewInit, OnDestroy {
  state = signal<any>(null);
  detail = signal<any>(null);
  health = signal<any>(null);
  cur: number | null = null;
  ovr = { open: false, did: null as number | null, cands: [] as any[], excluded: [] as any[] };

  vehicleId = '';
  message = '';
  mode = 'auto';
  submitting = false;
  learning = false;

  imageFile: File | null = null;
  imagePreview = '';
  audioBlob: Blob | null = null;
  recInfo = '';
  recLabel = 'Record voice note';
  recOn = false;

  sparkPoints = '';
  sparkAreaPoints = '';
  sparkLimitY = 0;
  sparkEnd: [number, number] | null = null;
  sparkInfo = '';
  sparkLatest = '';
  sparkDrift = '';
  sparkBreach = false;
  sparkShow = false;

  fleetFilter: 'all' | 'transit' | 'available' | 'reefer' = 'all';
  clockStr = '';

  map: any;
  layer: any;
  fitted = false;
  private timer: any;
  private clockTimer: any;
  private sigs: Record<string, string> = {};
  private rec: MediaRecorder | null = null;
  private chunks: Blob[] = [];

  presets = PRESETS;
  vehicleColor = (s: string) => VEHICLE_COLORS[s] || '#161410';
  statusClass = (s: string) => STATUS_CLASS[s] || '';

  ovrVehicle = '';
  ovrReason = '';

  constructor(private api: ApiService, private zone: NgZone) {}

  ngOnInit() {
    this.updateClock();
    this.clockTimer = setInterval(() => this.updateClock(), 1000);
    this.api.get('/api/health').then(h => this.health.set(h));
    this.refresh();
    this.timer = setInterval(() => this.refresh(), 1500);
  }

  ngOnDestroy() {
    clearInterval(this.timer);
    clearInterval(this.clockTimer);
  }

  private updateClock() {
    const now = new Date();
    const ist = new Date(now.getTime() + (5.5 * 60 - -now.getTimezoneOffset()) * 60000);
    const hh = String(ist.getUTCHours()).padStart(2, '0');
    const mm = String(ist.getUTCMinutes()).padStart(2, '0');
    const ss = String(ist.getUTCSeconds()).padStart(2, '0');
    this.clockStr = `${hh}:${mm}:${ss} IST`;
  }

  ngAfterViewInit() {
    this.map = L.map('map', { zoomControl: true }).setView([12.92, 78.85], 8);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: '&copy; OpenStreetMap contributors',
    }).addTo(this.map);
    this.layer = L.layerGroup().addTo(this.map);
  }

  get reportVehicles() {
    const s = this.state();
    return s ? s.vehicles.filter((v: any) => s.shipments.some((x: any) => x.vehicle_id === v.id)) : [];
  }

  get filteredVehicles() {
    const s = this.state();
    if (!s) return [];
    return s.vehicles.filter((v: any) => {
      if (this.fleetFilter === 'transit') return v.status === 'in_transit' || v.status === 'dispatched';
      if (this.fleetFilter === 'available') return v.status === 'available';
      if (this.fleetFilter === 'reefer') return v.temp_class === 'reefer';
      return true;
    });
  }

  get transitCount(): number {
    const s = this.state();
    return s ? s.vehicles.filter((v: any) => v.status === 'in_transit' || v.status === 'dispatched').length : 0;
  }

  get spareCount(): number {
    const s = this.state();
    return s ? s.vehicles.filter((v: any) => v.status === 'available').length : 0;
  }

  get openIncidentCount(): number {
    const s = this.state();
    return s ? s.incidents.filter((i: any) => i.status === 'running' || i.status === 'awaiting_approval').length : 0;
  }

  cargoOf(vid: string): string {
    const s = this.state();
    return s?.shipments.find((x: any) => x.vehicle_id === vid)?.cargo ?? 'Unassigned spare';
  }

  shipmentOf(vid: string): any {
    const s = this.state();
    return s?.shipments.find((x: any) => x.vehicle_id === vid) ?? null;
  }

  sparesAtDepot(depotId: string): string {
    const s = this.state();
    if (!s) return '0 spare';
    const units = s.vehicles.filter((v: any) => v.depot_id === depotId && v.status === 'available');
    return units.length ? units.map((u: any) => u.id).join(', ') : 'No spare idle';
  }

  applyPreset(p: PresetScenario) {
    this.vehicleId = p.veh;
    this.message = p.text;
    this.loadSpark();
  }

  focusVehicle(v: any) {
    if (this.reportVehicles.some((rv: any) => rv.id === v.id)) {
      this.vehicleId = v.id;
      this.loadSpark();
    }
    if (this.map && v.lat && v.lon) {
      this.map.setView([v.lat, v.lon], 10, { animate: true });
    }
  }

  fitCorridor() {
    const s = this.state();
    if (this.map && s?.depots?.length) {
      this.map.fitBounds(L.latLngBounds(s.depots.map((d: any) => [d.lat, d.lon])).pad(0.12));
    }
  }

  onVehiclesLoaded() {
    if (!this.vehicleId && this.reportVehicles.length) this.vehicleId = this.reportVehicles[0].id;
    if (this.vehicleId) this.loadSpark();
  }

  async refresh() {
    try {
      const s = await this.api.get('/api/state');
      const isNew = !this.state();
      this.state.set(s);
      if (isNew) this.onVehiclesLoaded();
      if (this.cur == null && s.incidents.length) this.cur = s.incidents[0].id;
      const d = this.cur != null ? await this.api.get('/api/incidents/' + this.cur) : null;
      this.detail.set(d && d.detail ? null : d);
      this.drawMap(s);
    } catch (e) {
      console.error(e);
    }
  }

  selectIncident(id: number) {
    this.cur = id;
    this.refresh();
  }

  pipelineStages(d: any): { label: string; state: 'done' | 'active' | 'idle' }[] {
    const trace: any[] = d?.trace || [];
    const decs: any[] = d?.decisions || [];
    const hasTriage = !!d?.triage || trace.some(e => e.author?.includes('triage') || e.name === 'triage');
    const hasScout = trace.some(e => e.name === 'find_alternate_vehicles' || e.author === 'scout_agent');
    const hasFanout = trace.some(e => String(e.author || '').startsWith('assess_')) || hasScout;
    const hasPlan = decs.length > 0 || trace.some(e => e.name === 'reroute' || e.author === 'planner_agent');
    const hasAction = decs.some(x => x.status === 'executed') || trace.some(e => e.name === 'log_decision');
    const running = d?.status === 'running';

    return [
      { label: '01 Triage', state: hasTriage ? 'done' : running ? 'active' : 'idle' },
      { label: '02 Scout', state: hasScout ? 'done' : hasTriage && running ? 'active' : 'idle' },
      { label: '03 Assess', state: hasFanout ? 'done' : hasScout && running ? 'active' : 'idle' },
      { label: '04 Gate', state: hasPlan ? 'done' : hasFanout && running ? 'active' : 'idle' },
      { label: '05 Dispatch', state: hasAction ? 'done' : decs.some(x => x.status === 'pending_approval') ? 'active' : 'idle' },
    ];
  }

  drawMap(s: any) {
    if (!this.map) return;
    const sig = JSON.stringify([
      s.vehicles.map((v: any) => [v.id, v.status, v.lat, v.lon]),
      s.decisions.slice(0, 6).map((d: any) => [d.id, d.status]),
    ]);
    if (this.sigs['map'] === sig) return;
    this.sigs['map'] = sig;
    this.layer.clearLayers();

    // Draw corridor spine connecting depots
    if (s.depots?.length > 1) {
      L.polyline(
        s.depots.map((d: any) => [d.lat, d.lon]),
        { color: '#b3aa93', weight: 2, dashArray: '3 5', opacity: 0.7 }
      ).addTo(this.layer);
    }

    s.depots.forEach((d: any) =>
      L.circleMarker([d.lat, d.lon], {
        radius: 5.5,
        color: '#161410',
        weight: 2,
        fillColor: '#faf8f2',
        fillOpacity: 1,
      })
        .bindTooltip(`Depot: ${d.name} (${d.dock_slots} docks)`)
        .addTo(this.layer)
    );

    s.shipments
      .filter((x: any) => ['in_transit', 'recovering'].includes(x.status))
      .forEach((x: any) =>
        L.circleMarker([x.dest_lat, x.dest_lon], {
          radius: 4.5,
          color: '#161410',
          weight: 1.5,
          fillColor: '#6c6454',
          fillOpacity: 0.95,
        })
          .bindTooltip(`Consignee: ${x.dest_name}`)
          .addTo(this.layer)
      );

    s.vehicles.forEach((v: any) =>
      L.circleMarker([v.lat, v.lon], {
        radius: v.status === 'available' ? 7 : 9,
        color: '#161410',
        weight: 1.5,
        fillColor: this.vehicleColor(v.status),
        fillOpacity: 0.92,
      })
        .bindTooltip(`${v.id} · ${v.driver} · ${v.temp_class} · ${v.status}`)
        .addTo(this.layer)
    );

    s.decisions
      .slice(0, 3)
      .filter((d: any) => ['executed', 'pending_approval'].includes(d.status) && d.route)
      .forEach((d: any) => {
        const r = JSON.parse(d.route);
        const pend = d.status === 'pending_approval';
        if (r[0]) {
          L.polyline(r[0], {
            color: '#8f5400',
            weight: 4,
            dashArray: '7 6',
            opacity: pend ? 0.55 : 0.95,
          }).addTo(this.layer);
        }
        if (r[1]) {
          L.polyline(r[1], {
            color: '#196134',
            weight: 4,
            opacity: pend ? 0.55 : 0.95,
          }).addTo(this.layer);
        }
        if (d.meet_lat) {
          L.circleMarker([d.meet_lat, d.meet_lon], {
            radius: 7.5,
            color: '#161410',
            weight: 2,
            fillColor: '#f0a824',
            fillOpacity: 1,
          })
            .bindTooltip(`Handover point: ${d.meet_label}`)
            .addTo(this.layer);
        }
      });

    if (!this.fitted && s.depots.length) {
      this.map.fitBounds(L.latLngBounds(s.depots.map((d: any) => [d.lat, d.lon])).pad(0.12));
      this.fitted = true;
    }
  }

  async loadSpark() {
    if (!this.vehicleId) return;
    const rows = await this.api.get('/api/telemetry/' + this.vehicleId);
    const t: number[] = rows.map((r: any) => r.temp_c);
    if (!t.length) {
      this.sparkShow = false;
      this.sparkInfo = 'No sensor readings recorded';
      this.sparkLatest = '';
      return;
    }
    const latest = t[t.length - 1];
    const isCold = latest < 20;
    this.sparkShow = isCold;
    if (!isCold) {
      this.sparkLatest = `${latest.toFixed(1)}°C`;
      this.sparkDrift = 'Ambient van';
      this.sparkBreach = false;
      this.sparkInfo = 'Ambient dry cargo class (no active cold-chain threshold)';
      return;
    }
    const mn = Math.min(...t, 0);
    const mx = Math.max(...t, 9.5);
    const y = (v: number) => 68 - ((v - mn) / (mx - mn)) * 58;
    this.sparkLimitY = y(8);
    const pts = t.map((v: number, i: number) => `${((i / (t.length - 1)) * 300).toFixed(1)},${y(v).toFixed(1)}`);
    this.sparkPoints = pts.join(' ');
    this.sparkAreaPoints = `0,74 ${pts.join(' ')} 300,74`;
    this.sparkEnd = [300, y(latest)];
    const drift = (latest - t[0]) / Math.max(1, t.length - 1);
    this.sparkLatest = `${latest.toFixed(1)}°C`;
    this.sparkDrift = `${drift >= 0 ? '+' : ''}${drift.toFixed(2)}°C/min`;
    this.sparkBreach = latest > 8.0 || drift > 0.1;
    this.sparkInfo = `Latest ${latest.toFixed(1)}°C · Limit 8.0°C · 40-min window`;
  }

  onImage(ev: Event) {
    const f = (ev.target as HTMLInputElement).files?.[0] ?? null;
    this.imageFile = f;
    this.imagePreview = f ? URL.createObjectURL(f) : '';
  }

  clearImage() {
    this.imageFile = null;
    this.imagePreview = '';
  }

  async toggleRec() {
    if (this.rec) {
      this.rec.stop();
      return;
    }
    try {
      const st = await navigator.mediaDevices.getUserMedia({ audio: true });
      this.chunks = [];
      this.rec = new MediaRecorder(st);
      this.rec.ondataavailable = e => this.chunks.push(e.data);
      this.rec.onstop = async () => {
        st.getTracks().forEach(t => t.stop());
        this.rec = null;
        this.audioBlob = await this.toWav(new Blob(this.chunks));
        this.zone.run(() => {
          this.recOn = false;
          this.recLabel = 'Record voice note';
          this.recInfo = `WAV ${(this.audioBlob!.size / 32000).toFixed(1)}s attached`;
        });
      };
      this.rec.start();
      this.recOn = true;
      this.recLabel = 'Stop recording';
    } catch (e: any) {
      alert('Microphone unavailable: ' + e.message);
    }
  }

  private async toWav(blob: Blob): Promise<Blob> {
    const ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
    const a = await ctx.decodeAudioData(await blob.arrayBuffer());
    const off = new OfflineAudioContext(1, Math.ceil(a.duration * 16000), 16000);
    const s = off.createBufferSource();
    s.buffer = a;
    s.connect(off.destination);
    s.start();
    const pcm = (await off.startRendering()).getChannelData(0);
    const o = new DataView(new ArrayBuffer(44 + pcm.length * 2));
    const w = (p: number, t: string) => [...t].forEach((c, i) => o.setUint8(p + i, c.charCodeAt(0)));
    w(0, 'RIFF');
    o.setUint32(4, 36 + pcm.length * 2, true);
    w(8, 'WAVEfmt ');
    o.setUint32(16, 16, true);
    o.setUint16(20, 1, true);
    o.setUint16(22, 1, true);
    o.setUint32(24, 16000, true);
    o.setUint32(28, 32000, true);
    o.setUint16(32, 2, true);
    o.setUint16(34, 16, true);
    w(36, 'data');
    o.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) {
      const v = Math.max(-1, Math.min(1, pcm[i]));
      o.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
    }
    return new Blob([o], { type: 'audio/wav' });
  }

  async submit() {
    if (!this.vehicleId || this.submitting) return;
    this.submitting = true;
    try {
      const fd = new FormData();
      fd.append('vehicle_id', this.vehicleId);
      fd.append('text', this.message);
      fd.append('mode', this.mode);
      if (this.imageFile) fd.append('image', this.imageFile);
      if (this.audioBlob) fd.append('audio', this.audioBlob, 'note.wav');
      const j = await this.api.postForm('/api/incidents', fd);
      this.cur = j.id;
      this.audioBlob = null;
      this.recInfo = '';
      this.message = '';
      this.imageFile = null;
      this.imagePreview = '';
      await this.refresh();
    } finally {
      this.submitting = false;
    }
  }

  async act(action: string, id: number) {
    await this.api.post(`/api/decisions/${id}/${action}`);
    this.refresh();
  }

  async rate(id: number, v: number) {
    await this.api.post(`/api/decisions/${id}/rate`, { rating: v });
    this.refresh();
  }

  async openOverride(id: number) {
    const r = await this.api.get(`/api/decisions/${id}/candidates`);
    this.ovr = {
      open: true,
      did: id,
      cands: r.candidates || [],
      excluded: r.excluded || [],
    };
    this.ovrVehicle = this.ovr.cands[0]?.vehicle_id || '';
  }

  async doOverride() {
    const r = await this.api.post(`/api/decisions/${this.ovr.did}/override`, {
      vehicle_id: this.ovrVehicle,
      reason: this.ovrReason,
    });
    if (r.error) alert(r.error);
    this.ovr = { open: false, did: null, cands: [], excluded: [] };
    this.ovrVehicle = '';
    this.ovrReason = '';
    this.refresh();
  }

  cancelOverride() {
    this.ovr = { open: false, did: null, cands: [], excluded: [] };
  }

  async learnNow() {
    this.learning = true;
    try {
      await this.api.post('/api/learn');
      await this.refresh();
    } finally {
      this.learning = false;
    }
  }

  async resetAll() {
    if (!confirm('Reset corridor fleet, telemetry, and incident ledger?')) return;
    await this.api.post('/api/reset');
    this.cur = null;
    this.detail.set(null);
    this.sigs = {};
    this.fitted = false;
    this.refresh();
  }

  fmt(v: any): string {
    return (+v || 0).toFixed(2);
  }

  hasDecision(d: any, did: number | null): boolean {
    return !!d?.decisions?.some((x: any) => x.id === did);
  }

  json(v: any, short = 0): string {
    const s = JSON.stringify(v, null, 1);
    return short ? String(JSON.stringify(v)).slice(0, short) : s;
  }
}
