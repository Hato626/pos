import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { TranslateModule, TranslateService } from '@ngx-translate/core';
import { finalize, forkJoin } from 'rxjs';
import { ApiService } from '../services/api.service';
import { LanguagePickerComponent } from '../shared/language-picker.component';
import { PlatformOperationsService, Restaurant, Settings, Finance, Ledger, Payment, Audit, Page } from './platform-operations.service';

type Section='overview'|'restaurants'|'finance'|'payments'|'settings'|'audit';
@Component({
  selector:'app-platform-console',standalone:true,
  imports:[CommonModule,FormsModule,TranslateModule,LanguagePickerComponent],
  templateUrl:'./platform-console.component.html',styleUrl:'./platform-console.component.scss',
})
export class PlatformConsoleComponent implements OnInit {
  private api=inject(PlatformOperationsService);
  private auth=inject(ApiService);
  private router=inject(Router);
  private translate=inject(TranslateService);
  section=signal<Section>('overview');
  sections:Section[]=['overview','restaurants','finance','payments','settings','audit'];
  busy=signal(false); error=signal(''); notice=signal('');
  cfg=signal<Settings|null>(null); report=signal<Finance|null>(null);
  restaurants=signal<Page<Restaurant>>({total:0,items:[]}); entries=signal<Page<Ledger>>({total:0,items:[]});
  payments=signal<Page<Payment>>({total:0,items:[]}); audits=signal<Page<Audit>>({total:0,items:[]});
  email=signal(''); search=''; statusFilter='all'; restaurantOffset=0; ledgerOffset=0; paymentOffset=0; auditOffset=0;
  currency='CNY'; start=new Date(Date.now()-29*86400000).toISOString().slice(0,10); end=new Date().toISOString().slice(0,10);
  tenantFilter=''; restaurantForm:Restaurant|null=null; overrideRate=false; restaurantRate=0; ownerPassword='';
  createOpen=false; newRestaurant={name:'',owner_name:'',owner_email:'',owner_password:'',currency:'CNY'};
  settingsForm:Settings|null=null; defaultRate=0; estimateRate=0; estimateFixed=0;
  adyenEnv:'test'|'live'='test';
  adyenForm={merchant_account:'',balance_platform:'',liable_balance_account:'',live_prefix:'',api_key:'',balance_api_key:'',hmac_key:'',clear_api_key:false,clear_balance_api_key:false,clear_hmac_key:false};
  refundPayment:Payment|null=null; refundAmount=0; refundReason=''; refundKey='';
  feeEntry:Ledger|null=null; feeAmount=0; feeNote='';

  ngOnInit() {
    this.auth.getPlatformMe().subscribe({next:p=>this.email.set(p.email),error:e=>this.fail(e)});
    this.api.settings().subscribe({next:c=>this.applySettings(c),error:e=>this.fail(e)});
    this.refresh();
  }
  params():Record<string,string|number> {
    return {start:this.start,end:this.end,currency:this.currency,...(this.tenantFilter?{tenant_id:Number(this.tenantFilter)}:{})};
  }
  open(section:Section) { this.section.set(section);this.error.set('');this.notice.set('');this.refresh(); }
  refresh() {
    this.busy.set(true);this.error.set('');
    const done=()=>this.busy.set(false);
    switch(this.section()) {
      case 'overview':case 'finance':
        forkJoin({report:this.api.finance(this.params()),ledger:this.api.ledger({...this.params(),offset:this.ledgerOffset})}).pipe(finalize(done)).subscribe({next:r=>{this.report.set(r.report);this.entries.set(r.ledger);},error:e=>this.fail(e)});break;
      case 'restaurants':this.api.restaurants(this.search,this.statusFilter,this.restaurantOffset).pipe(finalize(done)).subscribe({next:r=>this.restaurants.set(r),error:e=>this.fail(e)});break;
      case 'payments':this.api.payments(this.paymentOffset).pipe(finalize(done)).subscribe({next:r=>this.payments.set(r),error:e=>this.fail(e)});break;
      case 'audit':this.api.audit(this.auditOffset).pipe(finalize(done)).subscribe({next:r=>this.audits.set(r),error:e=>this.fail(e)});break;
      case 'settings':this.api.settings().pipe(finalize(done)).subscribe({next:r=>this.applySettings(r),error:e=>this.fail(e)});break;
    }
  }
  fail(e:unknown) {
    const error=e as {error?:{detail?:unknown}};
    this.error.set(typeof error?.error?.detail==='string'?error.error.detail:this.translate.instant('OPS.ERROR'));
  }
  success(key='SAVED') { this.notice.set(this.translate.instant('OPS.'+key));this.error.set(''); }
  money(value:number,currency=this.currency) { return new Intl.NumberFormat(this.translate.currentLang||'zh-CN',{style:'currency',currency}).format((value||0)/100); }
  percent(bps:number) { return (bps/100).toFixed(2)+'%'; }
  state(value:string) { const key='OPS.STATES.'+value;const text=this.translate.instant(key);return text===key?value:text; }
  action(value:string) { const key='OPS.ACTIONS.'+value.replaceAll('.','_');const text=this.translate.instant(key);return text===key?value:text; }
  bar(value:number) { const max=Math.max(1,...(this.report()?.daily||[]).map(x=>Math.abs(x.commission_cents)));return Math.max(2,Math.abs(value)/max*100); }
  page(kind:'restaurants'|'ledger'|'payments'|'audit',direction:number) {
    if(kind==='restaurants')this.restaurantOffset=Math.max(0,this.restaurantOffset+20*direction);
    if(kind==='ledger')this.ledgerOffset=Math.max(0,this.ledgerOffset+50*direction);
    if(kind==='payments')this.paymentOffset=Math.max(0,this.paymentOffset+30*direction);
    if(kind==='audit')this.auditOffset=Math.max(0,this.auditOffset+50*direction);
    this.refresh();
  }
  editRestaurant(r:Restaurant) {
    this.restaurantForm={...r};this.overrideRate=r.commission_bps!==null;this.restaurantRate=(r.commission_bps??r.effective_commission_bps)/100;this.ownerPassword='';
  }
  saveRestaurant() {
    const r=this.restaurantForm;if(!r)return;
    if(r.status==='suspended'&&!window.confirm(this.translate.instant('OPS.SUSPEND_CONFIRM')))return;
    const fields=['name','email','phone','address','city','status','notes','recipient_name','bank_name','account_last4','test_balance_account','live_balance_account','test_account_holder','live_account_holder','test_transfer_instrument','live_transfer_instrument'] as const;
    const body:Record<string,unknown>={commission_bps:this.overrideRate?Math.round(this.restaurantRate*100):null};
    fields.forEach(k=>body[k]=r[k]);
    this.busy.set(true);this.api.saveRestaurant(r.id,body).pipe(finalize(()=>this.busy.set(false))).subscribe({next:()=>{this.restaurantForm=null;this.success();this.refresh();},error:e=>this.fail(e)});
  }
  createRestaurant() {
    this.busy.set(true);this.api.createRestaurant(this.newRestaurant).pipe(finalize(()=>this.busy.set(false))).subscribe({next:()=>{this.createOpen=false;this.newRestaurant={name:'',owner_name:'',owner_email:'',owner_password:'',currency:'CNY'};this.success();this.refresh();},error:e=>this.fail(e)});
  }
  resetPassword() {
    if(!this.restaurantForm||!this.ownerPassword||!window.confirm(this.translate.instant('OPS.RESET_CONFIRM')))return;
    this.busy.set(true);this.api.resetPassword(this.restaurantForm.id,this.ownerPassword).pipe(finalize(()=>this.busy.set(false))).subscribe({next:()=>{this.ownerPassword='';this.success();},error:e=>this.fail(e)});
  }
  verifyRestaurant() {
    if(!this.restaurantForm)return;this.busy.set(true);
    this.api.verifyRestaurant(this.restaurantForm.id,this.adyenEnv).pipe(finalize(()=>this.busy.set(false))).subscribe({next:()=>this.success('VERIFIED'),error:e=>this.fail(e)});
  }
  applySettings(c:Settings) {
    this.cfg.set(c);this.settingsForm={...c};this.defaultRate=c.default_commission_bps/100;this.estimateRate=c.estimated_fee_bps/100;this.estimateFixed=c.estimated_fee_fixed_cents/100;this.selectAdyenEnv();
  }
  saveSettings() {
    const c=this.settingsForm;if(!c)return;
    const body={name:c.name,support_email:c.support_email,support_phone:c.support_phone,terms_url:c.terms_url,public_base_url:c.public_base_url,
      default_commission_bps:Math.round(this.defaultRate*100),estimated_fee_bps:Math.round(this.estimateRate*100),estimated_fee_fixed_cents:Math.round(this.estimateFixed*100),
      payment_enabled:c.payment_enabled,adyen_environment:c.adyen_environment,registration_enabled:c.registration_enabled};
    this.busy.set(true);this.api.saveSettings(body).pipe(finalize(()=>this.busy.set(false))).subscribe({next:r=>{this.applySettings(r);this.success();},error:e=>this.fail(e)});
  }
  selectAdyenEnv() {
    const c=this.cfg()?.adyen[this.adyenEnv];if(!c)return;
    this.adyenForm={merchant_account:c.merchant_account,balance_platform:c.balance_platform,liable_balance_account:c.liable_balance_account,live_prefix:c.live_prefix,
      api_key:'',balance_api_key:'',hmac_key:'',clear_api_key:false,clear_balance_api_key:false,clear_hmac_key:false};
  }
  saveAdyen() {
    this.busy.set(true);this.api.saveAdyen(this.adyenEnv,this.adyenForm).pipe(finalize(()=>this.busy.set(false))).subscribe({next:r=>{
      const cfg=this.cfg();if(cfg){cfg.adyen[this.adyenEnv]=r;this.cfg.set({...cfg});}this.selectAdyenEnv();this.success();
    },error:e=>this.fail(e)});
  }
  verifyAdyen() { this.busy.set(true);this.api.verifyAdyen(this.adyenEnv).pipe(finalize(()=>this.busy.set(false))).subscribe({next:()=>this.success('VERIFIED'),error:e=>this.fail(e)}); }
  webhookUrl() { return (this.settingsForm?.public_base_url||window.location.origin)+(this.cfg()?.webhook_path||''); }
  refundable(p:Payment) {return p.amount_cents-p.refunds.filter(r=>['creating','pending','refunded'].includes(r.state)).reduce((s,r)=>s+r.amount_cents,0);}
  openRefund(p:Payment) { this.refundPayment=p;this.refundAmount=this.refundable(p)/100;this.refundReason='';this.refundKey=crypto.randomUUID(); }
  submitRefund() {
    if(!this.refundPayment)return;this.busy.set(true);
    this.api.refund(this.refundPayment.id,{amount_cents:Math.round(this.refundAmount*100),reason:this.refundReason,idempotency_key:this.refundKey}).pipe(finalize(()=>this.busy.set(false))).subscribe({next:()=>{this.refundPayment=null;this.success('REFUND_SUBMITTED');this.refresh();},error:e=>this.fail(e)});
  }
  openFee(row:Ledger) {this.feeEntry=row;this.feeAmount=(row.actual_fee_cents??row.estimated_fee_cents)/100;this.feeNote='';}
  saveFee() {
    if(!this.feeEntry)return;this.busy.set(true);
    this.api.fee(this.feeEntry.id,Math.round(this.feeAmount*100),this.feeNote).pipe(finalize(()=>this.busy.set(false))).subscribe({next:()=>{this.feeEntry=null;this.success();this.refresh();},error:e=>this.fail(e)});
  }
  exportLedger() {this.api.export(this.params()).subscribe({next:blob=>{const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='platform-ledger.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);},error:e=>this.fail(e)});}
  logout(){this.auth.logout().subscribe(()=>this.router.navigate(['/platform/login']));}
}
