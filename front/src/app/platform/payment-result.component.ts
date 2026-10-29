import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { TranslateModule } from '@ngx-translate/core';
import { Subscription, timer, exhaustMap, takeWhile } from 'rxjs';
import { PlatformOperationsService, PaymentState } from './platform-operations.service';

@Component({
  selector: 'app-payment-result', standalone: true, imports: [RouterLink, TranslateModule],
  template: `<main><section aria-live="polite"><h1>{{ 'OPS.PAYMENT_RESULT' | translate }}</h1>
    @if (payment(); as p) {
      <p>{{ ('OPS.STATES.'+p.state) | translate }}</p>
      <p>{{ 'OPS.ORDER' | translate }} #{{ p.order_id }} · {{ (p.amount_cents/100).toFixed(2) }} {{ p.currency }}</p>
      @if (p.state==='captured') {<p>{{ 'OPS.PAYMENT_CONFIRMED' | translate }}</p>}
      @else if (p.state==='needs_review') {<p>{{ 'OPS.PAYMENT_REVIEW' | translate }}</p>}
      @else {<p>{{ 'OPS.PAYMENT_WAIT_HINT' | translate }}</p>}
    } @else {<p>{{ 'COMMON.LOADING' | translate }}</p>}
    @if (error()) {<p role="alert">{{ 'OPS.PAYMENT_QUERY_ERROR' | translate }}</p>}
    <button type="button" (click)="refresh()">{{ 'OPS.REFRESH' | translate }}</button>
    @if (token) {<a [routerLink]="['/menu',token]">{{ 'OPS.BACK_TO_MENU' | translate }}</a>}
  </section></main>`,
  styles: [`main{min-height:100dvh;background:#f5f4f1;display:grid;place-items:center;padding:24px;box-sizing:border-box;color:#20332d}section{background:#fff;padding:32px;border-radius:20px;width:min(100%,460px);box-sizing:border-box;box-shadow:0 12px 50px #20332d14}h1{font-size:24px}p{line-height:1.7}button,a{display:inline-block;padding:12px 18px;border-radius:9px;margin:12px 12px 0 0;border:1px solid #cad8d1;color:#20332d;background:#fff;text-decoration:none;cursor:pointer}`]
})
export class PaymentResultComponent implements OnInit, OnDestroy {
  private route=inject(ActivatedRoute); private api=inject(PlatformOperationsService);
  private subscription?:Subscription;
  payment=signal<PaymentState|null>(null); error=signal(false);
  token=this.route.snapshot.queryParamMap.get('table_token')||'';
  ngOnInit(){this.refresh();}
  refresh(){
    this.subscription?.unsubscribe();this.error.set(false);
    const id=this.route.snapshot.paramMap.get('paymentId');
    if(!id||!this.token){this.error.set(true);return;}
    this.subscription=timer(0,3000).pipe(
      exhaustMap(()=>this.api.paymentStatus(id,this.token)),
      takeWhile((p,i)=>i<39 && ['creating','pending','authorised'].includes(p.state),true)
    ).subscribe({next:p=>this.payment.set(p),error:()=>this.error.set(true)});
  }
  ngOnDestroy(){this.subscription?.unsubscribe();}
}
