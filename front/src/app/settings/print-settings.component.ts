import { Component, DestroyRef, inject, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { TranslateModule, TranslateService } from '@ngx-translate/core';
import { firstValueFrom, forkJoin, Subscription, switchMap, take, takeWhile, timer } from 'rxjs';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ApiService, PrintAgent } from '../services/api.service';
import { environment } from '../../environments/environment';

interface PrinterBinding {
  agent_id: number | null;
  kitchen_printer: string;
  receipt_printer: string;
  auto_kitchen: boolean;
  auto_receipt: boolean;
}
interface PrinterDevice {
  id: number;
  display_name: string;
  last_seen_at: string | null;
  printers: string[] | null;
  protocol_version?: number;
}
interface PaperSetting { agent_id: number; printer: string; width_mm: number; height_mm: number; }
interface BindingResponse { binding: PrinterBinding; devices: PrinterDevice[]; paper_settings: PaperSetting[]; }
interface WorkflowJob {
  id: number;
  order_id: number | null;
  job_type: string;
  title: string | null;
  status: string;
  printer: string | null;
  error_message: string | null;
  is_test?: boolean;
  paper?: {width_mm:number;height_mm:number} | null;
}

@Component({
  selector: 'app-print-settings',
  standalone: true,
  imports: [CommonModule, FormsModule, TranslateModule],
  template: `
    <section class="section" data-testid="settings-printing-section">
      <h2>{{ 'SETTINGS.PRINTING_TITLE' | translate }}</h2>
      <p class="hint">{{ 'PRINT_WORKFLOW.DESCRIPTION' | translate }}</p>
      <div class="flow-summary">
        <p><strong>{{ 'PRINT_WORKFLOW.KITCHEN_FLOW' | translate }}</strong><br>{{ 'PRINT_WORKFLOW.KITCHEN_DETAIL' | translate }}</p>
        <p><strong>{{ 'PRINT_WORKFLOW.RECEIPT_FLOW' | translate }}</strong><br>{{ 'PRINT_WORKFLOW.RECEIPT_DETAIL' | translate }}</p>
      </div>
      @if (error()) { <p class="notice error" role="alert">{{ error() }}</p> }
      @if (success()) { <p class="notice success" role="status">{{ success()! | translate:{id:testJobId()} }}</p> }
      <form (ngSubmit)="save()" #bindingForm="ngForm">
        <fieldset [disabled]="loading() || saving() || paperSaving() || testing() !== null || !loaded()">
          <legend>{{ 'PRINT_WORKFLOW.BINDING_TITLE' | translate }}</legend>
          <label for="print-device">{{ 'PRINT_WORKFLOW.DEVICE' | translate }}</label>
          <select id="print-device" name="print-device" [(ngModel)]="binding.agent_id" data-testid="workflow-device">
            <option [ngValue]="null">{{ 'PRINT_WORKFLOW.UNBOUND' | translate }}</option>
            @for (device of devices(); track device.id) {
              <option [ngValue]="device.id">{{ device.display_name }} #{{ device.id }} · {{ (online(device) ? 'PRINT_WORKFLOW.ONLINE' : 'PRINT_WORKFLOW.OFFLINE') | translate }}</option>
            }
          </select>
          <div class="printer-grid">
            <div>
              <label for="kitchen-printer">{{ 'PRINT_WORKFLOW.KITCHEN_PRINTER' | translate }}</label>
              <input id="kitchen-printer" name="kitchen-printer" list="workflow-printers" required maxlength="256" [(ngModel)]="binding.kitchen_printer" data-testid="workflow-kitchen">
              <label class="toggle"><input type="checkbox" name="auto-kitchen" [(ngModel)]="binding.auto_kitchen" data-testid="workflow-auto-kitchen">{{ 'PRINT_WORKFLOW.AUTO_KITCHEN' | translate }}</label>
            </div>
            <div>
              <label for="receipt-printer">{{ 'PRINT_WORKFLOW.RECEIPT_PRINTER' | translate }}</label>
              <input id="receipt-printer" name="receipt-printer" list="workflow-printers" required maxlength="256" [(ngModel)]="binding.receipt_printer" data-testid="workflow-receipt">
              <label class="toggle"><input type="checkbox" name="auto-receipt" [(ngModel)]="binding.auto_receipt" data-testid="workflow-auto-receipt">{{ 'PRINT_WORKFLOW.AUTO_RECEIPT' | translate }}</label>
            </div>
          </div>
          <datalist id="workflow-printers">@for (printer of printerNames(); track printer) { <option [value]="printer"></option> }</datalist>
          <p class="hint">{{ (binding.agent_id ? 'PRINT_WORKFLOW.QUEUE_HINT' : 'PRINT_WORKFLOW.PAIRING_NEEDED') | translate }}</p>
          <div class="actions">
            <button type="button" class="btn btn-secondary" (click)="samePrinter()" data-testid="workflow-same">{{ 'PRINT_WORKFLOW.SAME_PRINTER' | translate }}</button>
            <button type="submit" class="btn btn-primary" [disabled]="bindingForm.invalid || saving()" data-testid="workflow-save">{{ (saving() ? 'PRINT_WORKFLOW.SAVING' : 'PRINT_WORKFLOW.SAVE') | translate }}</button>
          </div>
        </fieldset>
      </form>
      <h3>{{ 'PRINT_WORKFLOW.PAPER_TITLE' | translate }}</h3>
      <p class="hint">{{ 'PRINT_WORKFLOW.PAPER_HINT' | translate }}</p>
      @if (!binding.agent_id) { <p class="hint">{{ 'PRINT_WORKFLOW.PAPER_CONNECT_FIRST' | translate }}</p> }
      @if (binding.agent_id) {
        <label for="paper-extra">{{ 'PRINT_WORKFLOW.PAPER_ADD' | translate }}</label>
        <select id="paper-extra" [ngModel]="''" (ngModelChange)="addPaper($event)" [disabled]="paperSaving() || loading()" data-testid="workflow-paper-add">
          <option value="">{{ 'PRINT_WORKFLOW.PAPER_ADD_PLACEHOLDER' | translate }}</option>
          @for (printer of printerNames(); track printer) { <option [value]="printer">{{ printer }}</option> }
        </select>
      }
      @for (printer of paperPrinters(); track printer; let index = $index) {
        <fieldset class="paper-row" [disabled]="loading() || saving() || paperSaving() || testing() !== null">
          <legend>{{ printer }}</legend>
          <label [for]="'paper-size-' + index">{{ 'PRINT_WORKFLOW.PAPER_SIZE' | translate }}</label>
          <select [id]="'paper-size-' + index" [ngModel]="paperValue(printer)" (ngModelChange)="setPaper(printer,$event)" [attr.data-testid]="'workflow-paper-size-' + index">
            @for (size of paperSizes; track size.value) { <option [value]="size.value">{{ size.label }}{{ size.legacy ? ('PRINT_WORKFLOW.PAPER_LEGACY' | translate) : '' }}</option> }
          </select>
          <div class="actions"><span class="hint">{{ (paperChanged(printer) ? 'PRINT_WORKFLOW.PAPER_UNSAVED' : 'PRINT_WORKFLOW.PAPER_CURRENT') | translate }} {{ paperValue(printer).replace('x',' × ') }} mm</span>
            <button type="button" class="btn btn-primary" (click)="savePaper(printer)" [disabled]="!paperChanged(printer) || !loaded()" [attr.data-testid]="'workflow-paper-save-' + index">{{ 'PRINT_WORKFLOW.PAPER_SAVE' | translate }}</button>
          </div>
        </fieldset>
      }
      @if (binding.agent_id && selectedDeviceVersion() < 2) {
        <p class="notice upgrade" data-testid="workflow-paper-upgrade">{{ 'PRINT_WORKFLOW.PAPER_UPGRADE' | translate }} <a href="/downloads/satisfecho-restaurant-windows.zip">{{ 'PRINT_WORKFLOW.DOWNLOAD' | translate }}</a></p>
      }
      <div class="actions">
        <button type="button" class="btn btn-secondary" (click)="testPrint('kitchen')" [disabled]="!canTest() || testPending('kitchen')" data-testid="workflow-test-kitchen">{{ (testing() === 'kitchen' ? 'PRINT_WORKFLOW.TEST_SENDING' : 'PRINT_WORKFLOW.TEST_KITCHEN') | translate }}</button>
        <button type="button" class="btn btn-secondary" (click)="testPrint('receipt')" [disabled]="!canTest() || testPending('receipt')" data-testid="workflow-test-receipt">{{ (testing() === 'receipt' ? 'PRINT_WORKFLOW.TEST_SENDING' : 'PRINT_WORKFLOW.TEST_RECEIPT') | translate }}</button>
      </div>
      <p class="hint" data-testid="workflow-test-hint">{{ (!savedBinding()?.agent_id ? 'PRINT_WORKFLOW.TEST_CONNECT_FIRST' : !bindingSaved() ? 'PRINT_WORKFLOW.TEST_SAVE_FIRST' : 'PRINT_WORKFLOW.TEST_HINT') | translate }}</p>
      <div class="actions">
        <button type="button" class="btn btn-secondary" (click)="reload()" [disabled]="loading() || saving() || paperSaving() || creating() || testing() !== null" data-testid="workflow-refresh">{{ 'PRINT_WORKFLOW.REFRESH' | translate }}</button>
        <a href="/downloads/receipt-60x40.html" target="_blank" rel="noopener">{{ 'PRINT_WORKFLOW.MANUAL_PRINT' | translate }}</a>
      </div>
      <h3>{{ 'PRINT_WORKFLOW.CONNECT_TITLE' | translate }}</h3>
      <ol class="instructions">
        <li>{{ 'PRINT_WORKFLOW.STEP_DRIVER' | translate }}</li>
        <li><a href="/downloads/satisfecho-restaurant-windows.zip">{{ 'PRINT_WORKFLOW.DOWNLOAD' | translate }}</a> {{ 'PRINT_WORKFLOW.STEP_APP' | translate }}</li>
        <li>{{ 'PRINT_WORKFLOW.STEP_TOKEN' | translate }}</li>
        <li>{{ 'PRINT_WORKFLOW.STEP_BIND' | translate }}</li>
      </ol>
      <div class="actions"><button type="button" class="btn btn-primary" (click)="createDevice()" [disabled]="creating() || saving() || paperSaving() || testing() !== null || !loaded()" data-testid="workflow-create">{{ 'PRINT_WORKFLOW.CREATE_TOKEN' | translate }}</button></div>
      @if (newToken()) {
        <div class="token-box" data-testid="print-agent-token-once">
          <p>{{ 'PRINT_WORKFLOW.TOKEN_ONCE' | translate }}</p><code>{{ newToken() }}</code>
          <button type="button" class="btn btn-secondary" (click)="copyToken()">{{ 'PRINT_WORKFLOW.COPY' | translate }}</button>
        </div>
      }
      <h3>{{ 'PRINT_WORKFLOW.DEVICES_TITLE' | translate }}</h3>
      @if (loaded() && !agents().length) { <p class="hint">{{ 'PRINT_WORKFLOW.NO_DEVICES' | translate }}</p> }
      <ul class="agent-list">
        @for (agent of agents(); track agent.id) {
          <li><span><strong>{{ agent.display_name }}</strong> #{{ agent.id }} · {{ (agent.revoked_at ? 'PRINT_WORKFLOW.REVOKED' : agent.online ? 'PRINT_WORKFLOW.ONLINE' : 'PRINT_WORKFLOW.OFFLINE') | translate }}</span>
            @if (!agent.revoked_at) { <button type="button" class="btn btn-secondary" (click)="revoke(agent)" [disabled]="saving() || loading() || paperSaving() || testing() !== null" [attr.data-testid]="'workflow-revoke-' + agent.id">{{ 'PRINT_WORKFLOW.REVOKE' | translate }}</button> }
          </li>
        }
      </ul>
      <h3>{{ 'PRINT_WORKFLOW.JOBS_TITLE' | translate }}</h3>
      <p class="hint">{{ 'PRINT_WORKFLOW.JOB_WARNING' | translate }}</p>
      @if (loaded() && !jobs().length) { <p class="hint" data-testid="workflow-no-jobs">{{ 'PRINT_WORKFLOW.NO_JOBS' | translate }}</p> }
      @if (jobs().length) {
        <div class="table-scroll"><table data-testid="workflow-jobs"><thead><tr><th>{{ 'PRINT_WORKFLOW.ORDER' | translate }}</th><th>{{ 'PRINT_WORKFLOW.STATUS' | translate }}</th><th>{{ 'PRINT_WORKFLOW.QUEUE' | translate }}</th><th>{{ 'PRINT_WORKFLOW.ACTION' | translate }}</th></tr></thead><tbody>
          @for (job of jobs(); track job.id) {
            <tr><td>{{ job.is_test ? ('PRINT_WORKFLOW.TEST_JOB' | translate) : '#' + job.order_id }} {{ job.title || ((job.job_type === 'kitchen' ? 'PRINT_WORKFLOW.KITCHEN' : 'PRINT_WORKFLOW.RECEIPT') | translate) }}</td>
              <td>{{ statusKey(job.status) | translate }} @if (job.error_message) { <small class="error">{{ job.error_message }}</small> }</td>
              <td>{{ job.printer || (job.job_type === 'kitchen' ? binding.kitchen_printer : binding.receipt_printer) }} @if (job.paper) { <small>{{ job.paper.width_mm }} × {{ job.paper.height_mm }} mm</small> }</td>
              <td>@if (job.status !== 'pending') { <button type="button" class="btn btn-secondary" (click)="reprint(job)" [disabled]="saving() || loading() || paperSaving() || testing() !== null" [attr.data-testid]="'workflow-reprint-' + job.id">{{ 'PRINT_WORKFLOW.REPRINT' | translate }}</button> }</td></tr>
          }
        </tbody></table></div>
      }
    </section>
  `,
  styles: [`
    h2 { margin: 0 0 .5rem; } h3 { margin: 1.8rem 0 .7rem; }
    .hint, .instructions { color: #64748b; font-size: .9rem; line-height: 1.7; }
    .flow-summary, .printer-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 1rem; }
    .flow-summary { margin: 1rem 0; padding: .6rem 1rem; border-radius: 8px; background: #f1f5f9; font-size: .9rem; line-height: 1.6; }
    fieldset { min-width: 0; border: 1px solid #cbd5e1; border-radius: 8px; padding: 1rem; }
    legend { padding: 0 .4rem; font-weight: 600; }
    label { display: block; margin: .7rem 0 .4rem; font-size: .9rem; font-weight: 600; }
    input:not([type=checkbox]), select { box-sizing: border-box; width: 100%; padding: .65rem; border: 1px solid #cbd5e1; border-radius: 6px; font: inherit; background: white; }
    .toggle { display: flex; gap: .5rem; align-items: flex-start; font-weight: 400; line-height: 1.6; }
    .toggle input { flex: none; margin-top: .4rem; }
    .actions { display: flex; gap: .7rem; align-items: center; flex-wrap: wrap; margin: 1rem 0; }
    .btn { padding: .55rem .8rem; border: 1px solid #cbd5e1; border-radius: 6px; font: inherit; cursor: pointer; }
    .btn-primary { color: white; background: #2563eb; border-color: #2563eb; } .btn-secondary { background: white; color: #334155; }
    button:disabled { opacity: .5; cursor: default; } a { color: #1d4ed8; }
    .notice { border-radius: 6px; padding: .75rem; } .error { color: #b91c1c; } .notice.error { background: #fef2f2; } .success { color: #166534; background: #f0fdf4; }
    .token-box { padding: 1rem; background: #fffbeb; border: 1px solid #fbbf24; border-radius: 8px; }
    .paper-row { margin: 1rem 0; } .paper-row legend { overflow-wrap: anywhere; } .upgrade { background: #fffbeb; color: #92400e; line-height: 1.7; }
    .token-box code { display: block; overflow-wrap: anywhere; margin: .8rem 0; user-select: all; }
    .agent-list { padding: 0; list-style: none; } .agent-list li { display: flex; justify-content: space-between; gap: 1rem; align-items: center; flex-wrap: wrap; padding: .7rem 0; border-bottom: 1px solid #e2e8f0; }
    .table-scroll { overflow-x: auto; } table { width: 100%; border-collapse: collapse; font-size: .85rem; } th,td { padding: .7rem; border-bottom: 1px solid #e2e8f0; text-align: left; } small { display: block; margin-top: .3rem; }
    @media(max-width:700px) { .flow-summary,.printer-grid { grid-template-columns: 1fr; } }
  `],
})
export class PrintSettingsComponent implements OnInit {
  private readonly http = inject(HttpClient);
  private readonly api = inject(ApiService);
  private readonly i18n = inject(TranslateService);
  private readonly destroyRef = inject(DestroyRef);
  private testPolling?: Subscription;
  private readonly base = environment.apiUrl.replace(/\/$/, '') + '/print-workflow';
  private readonly options = { headers: { 'X-Requested-With': 'SatisfechoPrinting' } };
  readonly loading = signal(false);
  readonly loaded = signal(false);
  readonly saving = signal(false);
  readonly creating = signal(false);
  readonly agents = signal<PrintAgent[]>([]);
  readonly devices = signal<PrinterDevice[]>([]);
  readonly jobs = signal<WorkflowJob[]>([]);
  readonly newToken = signal<string | null>(null);
  readonly error = signal<string | null>(null);
  readonly success = signal<string | null>(null);
  readonly testing = signal<'kitchen' | 'receipt' | null>(null);
  readonly testJobId = signal<number | null>(null);
  readonly savedBinding = signal<PrinterBinding | null>(null);
  readonly paperSaving = signal(false);
  readonly papers = signal<PaperSetting[]>([]);
  readonly paperDrafts = signal<Record<string,string>>({});
  readonly extraPaperPrinters = signal<Record<string,string[]>>({});
  readonly paperSizes = [
    {value:'50x60',label:'50 × 60 mm',legacy:false}, {value:'30x60',label:'30 × 60 mm',legacy:false},
    {value:'60x50',label:'60 × 50 mm',legacy:false}, {value:'60x30',label:'60 × 30 mm',legacy:false},
    {value:'60x40',label:'60 × 40 mm',legacy:true},
  ];
  binding: PrinterBinding = { agent_id:null, kitchen_printer:'', receipt_printer:'', auto_kitchen:false, auto_receipt:false };

  ngOnInit(): void { void this.reload(); }
  online(device: PrinterDevice): boolean { return !!device.last_seen_at && Date.now() - Date.parse(device.last_seen_at) < 60000; }
  printerNames(): string[] { return this.devices().find(device => device.id === this.binding.agent_id)?.printers ?? []; }
  samePrinter(): void { this.binding.receipt_printer = this.binding.kitchen_printer; }
  selectedDeviceVersion(): number { return this.devices().find(device => device.id === this.binding.agent_id)?.protocol_version ?? 1; }
  paperPrinters(): string[] { return !this.binding.agent_id ? [] : [...new Set([this.binding.kitchen_printer.trim(),this.binding.receipt_printer.trim(),...this.papers().filter(paper => paper.agent_id === this.binding.agent_id).map(paper => paper.printer),...(this.extraPaperPrinters()[String(this.binding.agent_id)] ?? [])])].filter(Boolean); }
  addPaper(printer: string): void { if (printer && this.printerNames().includes(printer)) this.extraPaperPrinters.update(values => ({...values,[String(this.binding.agent_id)]:[...(values[String(this.binding.agent_id)] ?? []),printer]})); }
  paperKey(printer: string): string { return JSON.stringify([this.binding.agent_id,printer]); }
  savedPaperValue(printer: string): string {
    const paper = this.papers().find(paper => paper.agent_id === this.binding.agent_id && paper.printer === printer);
    return paper ? paper.width_mm + 'x' + paper.height_mm : '60x40';
  }
  paperValue(printer: string): string { return this.paperDrafts()[this.paperKey(printer)] ?? this.savedPaperValue(printer); }
  setPaper(printer: string, value: string): void { this.paperDrafts.update(drafts => ({...drafts,[this.paperKey(printer)]:value})); }
  paperChanged(printer: string): boolean { return this.paperValue(printer) !== this.savedPaperValue(printer); }
  papersChanged(): boolean { return this.paperPrinters().some(printer => this.paperChanged(printer)); }
  async savePaper(printer: string): Promise<void> {
    if (!this.binding.agent_id || this.paperSaving() || !this.paperChanged(printer)) return;
    const [width_mm,height_mm] = this.paperValue(printer).split('x').map(Number);
    const paper: PaperSetting = {agent_id:this.binding.agent_id,printer,width_mm,height_mm};
    this.paperSaving.set(true); this.error.set(null); this.success.set(null);
    try {
      await firstValueFrom(this.http.put(this.base + '/paper-settings', paper, this.options));
      this.papers.update(papers => [...papers.filter(value => value.agent_id !== paper.agent_id || value.printer !== printer),paper]);
      this.success.set('PRINT_WORKFLOW.PAPER_SAVED');
    } catch(error) { this.showError(error); } finally { this.paperSaving.set(false); }
  }
  bindingSaved(): boolean {
    const saved = this.savedBinding();
    return !!saved && !this.papersChanged() && (['agent_id','kitchen_printer','receipt_printer','auto_kitchen','auto_receipt'] as const).every(key => this.binding[key] === saved[key]);
  }
  canTest(): boolean { return this.loaded() && !!this.savedBinding()?.agent_id && this.bindingSaved() && !this.loading() && !this.saving() && !this.paperSaving() && !this.creating() && !this.testing(); }
  testPending(kind: string): boolean { return this.jobs().some(job => job.is_test && job.job_type === kind && (job.status === 'pending' || job.status === 'claimed')); }
  async testPrint(kind: 'kitchen' | 'receipt'): Promise<void> {
    if (!this.canTest() || this.testPending(kind)) return;
    this.testing.set(kind); this.error.set(null); this.success.set(null);
    try {
      const result = await firstValueFrom(this.http.post<{id:number}>(this.base + '/test-print', {job_type:kind}, this.options));
      this.testJobId.set(result.id);
      await this.reload(); this.success.set('PRINT_WORKFLOW.TEST_QUEUED');
      this.testPolling?.unsubscribe();
      this.testPolling = timer(2000,3000).pipe(
        switchMap(() => this.http.get<WorkflowJob[]>(this.base + '/jobs')),
        take(40),
        takeWhile(jobs => jobs.some(job => job.is_test && (job.status === 'pending' || job.status === 'claimed')), true),
        takeUntilDestroyed(this.destroyRef),
      ).subscribe({next:jobs => this.jobs.set(jobs),error:error => this.showError(error)});
    } catch(error) { this.showError(error); } finally { this.testing.set(null); }
  }
  statusKey(status: string): string {
    return ({pending:'PENDING',claimed:'CLAIMED',done:'DONE',failed:'FAILED',cancelled:'CANCELLED'} as Record<string,string>)[status]
      ? 'PRINT_WORKFLOW.' + ({pending:'PENDING',claimed:'CLAIMED',done:'DONE',failed:'FAILED',cancelled:'CANCELLED'} as Record<string,string>)[status]
      : 'PRINT_WORKFLOW.UNKNOWN';
  }
  private showError(error: unknown): void {
    const detail = error instanceof HttpErrorResponse ? error.error?.detail : null;
    this.error.set(typeof detail === 'string' ? detail : this.i18n.instant('PRINT_WORKFLOW.ERROR'));
  }
  async reload(): Promise<void> {
    if (this.loading()) return;
    this.loading.set(true); this.error.set(null);
    try {
      const result = await firstValueFrom(forkJoin({
        settings:this.http.get<BindingResponse>(this.base + '/binding'),
        jobs:this.http.get<WorkflowJob[]>(this.base + '/jobs'),
        agents:this.api.listPrintAgents(),
      }));
      this.binding = { ...result.settings.binding };
      this.savedBinding.set({ ...result.settings.binding });
      this.papers.set(result.settings.paper_settings ?? []); this.paperDrafts.set({});
      this.devices.set(result.settings.devices); this.jobs.set(result.jobs); this.agents.set(result.agents); this.loaded.set(true);
    } catch(error) {
      this.loaded.set(false); this.savedBinding.set(null); this.jobs.set([]); this.agents.set([]); this.devices.set([]); this.newToken.set(null); this.showError(error);
    } finally { this.loading.set(false); }
  }
  async save(): Promise<void> {
    if (!this.loaded() || this.saving()) return;
    this.saving.set(true); this.error.set(null); this.success.set(null);
    try {
      const binding = { ...this.binding, kitchen_printer:this.binding.kitchen_printer.trim(), receipt_printer:this.binding.receipt_printer.trim() };
      await firstValueFrom(this.http.put(this.base + '/binding', binding, this.options));
      this.binding = binding; this.savedBinding.set({ ...binding });
      this.success.set('PRINT_WORKFLOW.SAVED');
    } catch(error) { this.showError(error); } finally { this.saving.set(false); }
  }
  async createDevice(): Promise<void> {
    if (this.creating()) return;
    this.creating.set(true); this.error.set(null); this.success.set(null);
    try {
      const device = await firstValueFrom(this.http.post<{id:number;token:string}>(this.base + '/devices', {}, this.options));
      this.newToken.set(device.token); await this.reload();
    } catch(error) { this.showError(error); } finally { this.creating.set(false); }
  }
  async copyToken(): Promise<void> {
    try { await navigator.clipboard.writeText(this.newToken() ?? ''); this.success.set('PRINT_WORKFLOW.COPIED'); }
    catch { this.error.set(this.i18n.instant('PRINT_WORKFLOW.COPY_MANUALLY')); }
  }
  async revoke(agent: PrintAgent): Promise<void> {
    if (!window.confirm(this.i18n.instant('PRINT_WORKFLOW.REVOKE_CONFIRM', {name:agent.display_name}))) return;
    this.saving.set(true); this.error.set(null); this.success.set(null);
    try { await firstValueFrom(this.api.revokePrintAgent(agent.id)); this.newToken.set(null); await this.reload(); }
    catch(error) { this.showError(error); } finally { this.saving.set(false); }
  }
  async reprint(job: WorkflowJob): Promise<void> {
    if (!window.confirm(this.i18n.instant('PRINT_WORKFLOW.REPRINT_CONFIRM'))) return;
    this.saving.set(true); this.error.set(null); this.success.set(null);
    try { await firstValueFrom(this.http.post(this.base + '/jobs/' + job.id + '/reprint', {}, this.options)); await this.reload(); this.success.set('PRINT_WORKFLOW.REPRINT_QUEUED'); }
    catch(error) { this.showError(error); } finally { this.saving.set(false); }
  }
}
