import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { environment } from '../../environments/environment';

export interface Page<T> { total: number; items: T[]; }
export interface Restaurant {
  id: number; tenant_id: number; name: string; email: string; phone: string; address: string; city: string;
  public_slug: string; currency: string; status: 'active'|'suspended'; commission_bps: number|null;
  effective_commission_bps: number; owner_email: string; owner_name: string; notes: string;
  recipient_name: string; bank_name: string; account_last4: string;
  test_balance_account: string; live_balance_account: string; test_account_holder: string; live_account_holder: string;
  test_transfer_instrument: string; live_transfer_instrument: string;
}
export interface AdyenConfig {
  environment: 'test'|'live'; merchant_account: string; balance_platform: string; liable_balance_account: string;
  live_prefix: string; api_key_configured: boolean; balance_api_key_configured: boolean; hmac_key_configured: boolean; ready: boolean;
}
export interface Settings {
  name: string; support_email: string; support_phone: string; terms_url: string; public_base_url: string;
  default_commission_bps: number; estimated_fee_bps: number; estimated_fee_fixed_cents: number;
  payment_enabled: boolean; adyen_environment: 'test'|'live'; registration_enabled: boolean;
  adyen: {test: AdyenConfig; live: AdyenConfig}; secrets_storage_ready: boolean; allow_test_payments: boolean;
  webhook_path: string; currencies: string[];
}
export interface Totals {
  net_sales_cents: number; gross_sales_cents: number; reversals_cents: number; commission_cents: number;
  allocated_commission_cents: number; receivable_commission_cents: number; restaurant_share_cents: number;
  cost_cents: number; estimated_profit_cents: number; unconfirmed_cost_count: number; paid_order_count: number;
}
export interface Finance {
  currency: string; timezone: string; totals: Totals; restaurant_count: number; suspended_count: number; review_count: number;
  daily: (Totals & {day:string})[]; restaurants: (Totals & {tenant_id:number;name:string})[];
}
export interface Ledger {
  id:number; restaurant_name:string; order_id:number; currency:string; kind:string; channel:string;
  amount_cents:number; commission_cents:number; commission_bps:number; estimated_fee_cents:number;
  actual_fee_cents:number|null; occurred_at:string;
}
export interface Refund { id:string; amount_cents:number; reason:string; state:string; }
export interface Payment {
  id:string; order_id:number; restaurant_name:string; currency:string; amount_cents:number; commission_cents:number;
  state:string; environment:string; created_at:string; captured_at:string|null; psp_reference:string|null;
  review_reason:string|null; refunds:Refund[];
}
export interface Audit { id:number; actor_id:number; action:string; target:string; details:string; created_at:string; }
export interface PaymentState { id:string; state:string; checkout_url:string|null; amount_cents:number; currency:string; order_id:number; }

@Injectable({providedIn:'root'})
export class PlatformOperationsService {
  private http=inject(HttpClient);
  private root=environment.apiUrl+'/platform/operations';
  restaurants(search='',status='all',offset=0) { return this.http.get<Page<Restaurant>>(this.root+'/restaurants',{params:{search,status,offset,limit:20}}); }
  restaurant(id:number) { return this.http.get<Restaurant>(this.root+'/restaurants/'+id); }
  createRestaurant(body:object) { return this.http.post<Restaurant>(this.root+'/restaurants',body); }
  saveRestaurant(id:number,body:object) { return this.http.put<Restaurant>(this.root+'/restaurants/'+id,body); }
  resetPassword(id:number,password:string) { return this.http.post(this.root+'/restaurants/'+id+'/owner-password',{password}); }
  settings() { return this.http.get<Settings>(this.root+'/settings'); }
  saveSettings(body:object) { return this.http.put<Settings>(this.root+'/settings',body); }
  saveAdyen(env:string,body:object) { return this.http.put<AdyenConfig>(this.root+'/adyen/'+env,body); }
  verifyAdyen(env:string) { return this.http.post(this.root+'/adyen/'+env+'/verify',{}); }
  verifyRestaurant(id:number,env:string) { return this.http.post(this.root+'/restaurants/'+id+'/adyen/'+env+'/verify',{}); }
  finance(params:Record<string,string|number>) { return this.http.get<Finance>(this.root+'/finance',{params}); }
  ledger(params:Record<string,string|number>) { return this.http.get<Page<Ledger>>(this.root+'/ledger',{params}); }
  export(params:Record<string,string|number>) { return this.http.get(this.root+'/ledger/export',{params,responseType:'blob'}); }
  fee(id:number,actual_fee_cents:number,note:string) { return this.http.put(this.root+'/ledger/'+id+'/fee',{actual_fee_cents,note}); }
  payments(offset=0) { return this.http.get<Page<Payment>>(this.root+'/payments',{params:{offset}}); }
  refund(id:string,body:object) { return this.http.post<Refund>(this.root+'/payments/'+id+'/refunds',body); }
  audit(offset=0) { return this.http.get<Page<Audit>>(this.root+'/audit',{params:{offset}}); }
  paymentLink(orderId:number,table_token:string) { return this.http.post<PaymentState>(environment.apiUrl+'/platform-payments/orders/'+orderId+'/link',{table_token}); }
  paymentStatus(id:string,table_token:string) { return this.http.get<PaymentState>(environment.apiUrl+'/platform-payments/payments/'+id,{params:{table_token}}); }
}
