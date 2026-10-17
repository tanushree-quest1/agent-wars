import { AfterViewInit, Component, NgZone, OnDestroy, OnInit, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from './api.service';

declare const L: any;

const PRESETS: Record<string, [string, string]> = {
  'Reefer failing + crushed pallets': ['TRK-3', "Reefer unit has stopped cooling and the temperature display keeps climbing. Two pallets also got crushed when I braked hard. Truck is still drivable."],
  'Engine seized': ['TRK-5', "Engine seized near the toll plaza, truck won't start. Stuck on the highway."],
  'Road flooded': ['TRK-2', "Road ahead is flooded and police have blocked the highway, long jam."],
  'Vague report': ['TRK-1', "something is wrong with the truck, not sure what"],
};

const VEHICLE_COLORS: Record<string, string> = {
  in_transit: '#2b4f9e', available: '#22703f', out_of_service: '#c03a11', dispatched: '#8f5f00',
};

const STATUS_CLASS: Record<string, string> = {
  executed: 'ok', actioned: 'ok', pending_approval: 'warn', awaiting_approval: 'warn', running: 'warn',
  overridden: 'bad', needs_human: 'bad', rejected: 'bad', failed: 'bad',
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
  ovr = { open: false, did: null as number | null, cands: [] as any[] };

  vehicleId = '';
  message = '';
  mode = 'auto';
  imageFile: File | null = null;
  imagePreview = '';
  audioBlob: Blob | null = null;
  recInfo = '';
  recLabel = 'Record voice note';
  recOn = false;

  sparkPoints = '';
  sparkLimitY = 0;
  sparkEnd: [number, number] | null = null;
  sparkInfo = '';
  sparkShow = false;

  map: any;
  layer: any;
  fitted = false;
  private timer: any;
  private sigs: Record<string, string> = {};
  private rec: MediaRecorder | null = null;
  private chunks: Blob[] = [];

  presets = Object.entries(PRESETS).map(([label, [veh, text]]) => ({ label, veh, text }));
  vehicleColor = (s: string) => VEHICLE_COLORS[s] || '#211d15';
  statusClass = (s: string) => STATUS_CLASS[s] || '';

  constructor(private api: ApiService, private zone: NgZone) {}

  ngOnInit() {
    this.api.get('/api/health').then(h => this.health.set(h));
    this.refresh();
    this.timer = setInterval(() => this.refresh(), 1500);
  }

  ngOnDestroy() { clearInterval(this.timer); }

  ngAfterViewInit() {
    this.map = L.map('map').setView([12.95, 78.4], 8);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 18, attribution: '© OpenStreetMap' }).addTo(this.map);
    this.layer = L.layerGroup().addTo(this.map);
  }

  get reportVehicles() {
    const s = this.state();
    return s ? s.vehicles.filter((v: any) => s.shipments.some((x: any) => x.vehicle_id === v.id)) : [];
  }

  cargoOf(vid: string): string {
    const s = this.state();
    return s?.shipments.find((x: any) => x.vehicle_id === vid)?.cargo ?? '';
  }

  applyPreset(p: { veh: string; text: string }) {
    this.vehicleId = p.veh;
    this.message = p.text;
    this.loadSpark();
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
    } catch (e) { console.error(e); }
  }

  selectIncident(id: number) { this.cur = id; this.refresh(); }

  drawMap(s: any) {
    if (!this.map) return;
    const sig = JSON.stringify([s.vehicles.map((v: any) => v.status), s.decisions.slice(0, 6).map((d: any) => [d.id, d.status])]);
    if (this.sigs['map'] === sig) return;
    this.sigs['map'] = sig;
    this.layer.clearLayers();
    s.depots.forEach((d: any) =>
      L.circleMarker([d.lat, d.lon], { radius: 5, color: '#211d15', weight: 1.5, fillColor: '#fbfaf4', fillOpacity: 1 }).bindTooltip(d.name).addTo(this.layer));
    s.shipments.filter((x: any) => ['in_transit', 'recovering'].includes(x.status)).forEach((x: any) =>
      L.circleMarker([x.dest_lat, x.dest_lon], { radius: 4, color: '#6b4a9e', fillOpacity: .9 }).bindTooltip('Dest: ' + x.dest_name).addTo(this.layer));
    s.vehicles.forEach((v: any) =>
      L.circleMarker([v.lat, v.lon], { radius: 9, color: this.vehicleColor(v.status), weight: 2, fillOpacity: .9 })
        .bindTooltip(`${v.id} · ${v.driver} · ${v.temp_class} · ${v.status}`).addTo(this.layer));
    s.decisions.slice(0, 3).filter((d: any) => ['executed', 'pending_approval'].includes(d.status) && d.route).forEach((d: any) => {
      const r = JSON.parse(d.route), pend = d.status === 'pending_approval';
      if (r[0]) L.polyline(r[0], { color: '#8f5f00', weight: 4, dashArray: '7 6', opacity: pend ? .5 : 1 }).addTo(this.layer);
      if (r[1]) L.polyline(r[1], { color: '#22703f', weight: 4, opacity: pend ? .5 : 1 }).addTo(this.layer);
      if (d.meet_lat) L.circleMarker([d.meet_lat, d.meet_lon], { radius: 7, color: '#211d15', weight: 2, fillColor: '#f2b035', fillOpacity: 1 })
        .bindTooltip('Transfer: ' + d.meet_label).addTo(this.layer);
    });
    if (!this.fitted && s.depots.length) {
      this.map.fitBounds(L.latLngBounds(s.depots.map((d: any) => [d.lat, d.lon])).pad(.1));
      this.fitted = true;
    }
  }

  async loadSpark() {
    if (!this.vehicleId) return;
    const rows = await this.api.get('/api/telemetry/' + this.vehicleId);
    const t = rows.map((r: any) => r.temp_c);
    if (!t.length) { this.sparkShow = false; this.sparkInfo = ''; return; }
    const isCold = t[t.length - 1] < 20;
    this.sparkShow = isCold;
    if (!isCold) { this.sparkInfo = 'ambient cargo — no cold-chain telemetry'; return; }
    const mn = Math.min(...t, 0), mx = Math.max(...t, 9);
    const y = (v: number) => 66 - (v - mn) / (mx - mn) * 60;
    this.sparkLimitY = y(8);
    this.sparkPoints = t.map((v: number, i: number) => `${i / (t.length - 1) * 300},${y(v)}`).join(' ');
    this.sparkEnd = [300, y(t[t.length - 1])];
    this.sparkInfo = `latest ${t[t.length - 1].toFixed(1)}°C · limit 8°C (red line)`;
  }

  onImage(ev: Event) {
    const f = (ev.target as HTMLInputElement).files?.[0] ?? null;
    this.imageFile = f;
    this.imagePreview = f ? URL.createObjectURL(f) : '';
  }

  async toggleRec() {
    if (this.rec) { this.rec.stop(); return; }
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
          this.recInfo = `voice note ${(this.audioBlob!.size / 32000).toFixed(1)}s ready`;
        });
      };
      this.rec.start();
      this.recOn = true;
      this.recLabel = 'Stop';
    } catch (e: any) { alert('Mic unavailable: ' + e.message); }
  }

  private async toWav(blob: Blob): Promise<Blob> {
    const ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
    const a = await ctx.decodeAudioData(await blob.arrayBuffer());
    const off = new OfflineAudioContext(1, Math.ceil(a.duration * 16000), 16000);
    const s = off.createBufferSource(); s.buffer = a; s.connect(off.destination); s.start();
    const pcm = (await off.startRendering()).getChannelData(0);
    const o = new DataView(new ArrayBuffer(44 + pcm.length * 2));
    const w = (p: number, t: string) => [...t].forEach((c, i) => o.setUint8(p + i, c.charCodeAt(0)));
    w(0, 'RIFF'); o.setUint32(4, 36 + pcm.length * 2, true); w(8, 'WAVEfmt ');
    o.setUint32(16, 16, true); o.setUint16(20, 1, true); o.setUint16(22, 1, true);
    o.setUint32(24, 16000, true); o.setUint32(28, 32000, true);
    o.setUint16(32, 2, true); o.setUint16(34, 16, true);
    w(36, 'data'); o.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) {
      const v = Math.max(-1, Math.min(1, pcm[i]));
      o.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
    }
    return new Blob([o], { type: 'audio/wav' });
  }

  async submit() {
    const fd = new FormData();
    fd.append('vehicle_id', this.vehicleId);
    fd.append('text', this.message);
    fd.append('mode', this.mode);
    if (this.imageFile) fd.append('image', this.imageFile);
    if (this.audioBlob) fd.append('audio', this.audioBlob, 'note.wav');
    const j = await this.api.postForm('/api/incidents', fd);
    this.cur = j.id;
    this.audioBlob = null; this.recInfo = ''; this.message = ''; this.imageFile = null; this.imagePreview = '';
    this.refresh();
  }

  async act(action: string, id: number) { await this.api.post(`/api/decisions/${id}/${action}`); this.refresh(); }
  async rate(id: number, v: number) { await this.api.post(`/api/decisions/${id}/rate`, { rating: v }); this.refresh(); }

  async openOverride(id: number) {
    const r = await this.api.get(`/api/decisions/${id}/candidates`);
    this.ovr = { open: true, did: id, cands: r.candidates || [] };
    this.ovrVehicle = this.ovr.cands[0]?.vehicle_id || '';
  }

  ovrVehicle = '';
  ovrReason = '';

  async doOverride() {
    const r = await this.api.post(`/api/decisions/${this.ovr.did}/override`, { vehicle_id: this.ovrVehicle, reason: this.ovrReason });
    if (r.error) alert(r.error);
    this.ovr = { open: false, did: null, cands: [] };
    this.ovrVehicle = ''; this.ovrReason = '';
    this.refresh();
  }

  cancelOverride() { this.ovr = { open: false, did: null, cands: [] }; }

  async learnNow() { await this.api.post('/api/learn'); this.refresh(); }

  async resetAll() {
    if (!confirm('Reset fleet and all history?')) return;
    await this.api.post('/api/reset');
    this.cur = null; this.detail.set(null); this.sigs = {}; this.fitted = false;
    this.refresh();
  }

  fmt(v: any): string { return (+v || 0).toFixed(2); }
  hasDecision(d: any, did: number | null): boolean { return !!d?.decisions?.some((x: any) => x.id === did); }

  json(v: any, short = 0): string {
    const s = JSON.stringify(v, null, 1);
    return short ? String(JSON.stringify(v)).slice(0, short) : s;
  }
}
