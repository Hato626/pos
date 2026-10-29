import { Component, DestroyRef, OnInit, afterNextRender, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { Title } from '@angular/platform-browser';
import { TranslateModule, TranslateService } from '@ngx-translate/core';
import { forkJoin, merge, of } from 'rxjs';
import { catchError, tap } from 'rxjs/operators';
import { ApiService, LoyaltyProgramPublic, TenantSummary } from '../services/api.service';
import { LanguagePickerComponent } from '../shared/language-picker.component';
import { PublicGuestHeaderComponent } from '../shared/public-guest-header.component';
import { PublicGuestSalesCtasComponent } from '../shared/public-guest-sales-ctas.component';
import { LegalLinksComponent } from '../shared/legal-links.component';
import { contactEmailValid, contactPhoneValid } from '../shared/contact-validators';
import { resolvePublicPrimaryColor } from '../shared/public-brand-colors';

@Component({
  selector: 'app-loyalty-public',
  standalone: true,
  imports: [
    FormsModule,
    TranslateModule,
    LanguagePickerComponent,
    PublicGuestHeaderComponent,
    PublicGuestSalesCtasComponent,
    LegalLinksComponent,
    RouterLink,
  ],
  templateUrl: './loyalty-public.component.html',
  styleUrls: ['../book/book.component.scss', './loyalty-public.component.scss'],
})
export class LoyaltyPublicComponent implements OnInit {
  readonly resolvePublicPrimaryColor = resolvePublicPrimaryColor;
  private route = inject(ActivatedRoute);
  private api = inject(ApiService);
  private translate = inject(TranslateService);
  private title = inject(Title);
  private destroyRef = inject(DestroyRef);

  tenantId = signal(0);
  tenant = signal<TenantSummary | null>(null);
  logoUrl = signal<string | null>(null);
  program = signal<LoyaltyProgramPublic | null>(null);
  loading = signal(true);
  errorKind = signal<'missing_link' | 'invalid_tenant' | 'not_enabled' | null>(null);
  submitting = signal(false);
  submitted = signal(false);
  submitError = signal<string | null>(null);
  memberToken = signal<string | null>(null);
  balance = signal(0);
  applePkpassUrl = signal<string | null>(null);
  googleSaveUrl = signal<string | null>(null);
  origin =
    typeof window !== 'undefined' && window.location?.origin
      ? window.location.origin
      : '';

  displayName = '';
  email = '';
  phone = '';
  birthdayMonth: number | null = null;
  birthdayDay: number | null = null;
  referralCode = '';
  vipTier = signal<string | null>(null);
  ownReferralCode = signal<string | null>(null);
  recovered = signal(false);
  recoverEmail = '';
  recoverPhone = '';
  recovering = signal(false);
  recoverError = signal<string | null>(null);

  constructor() {
    afterNextRender(() => this.updateDocumentTitle());
  }

  ngOnInit(): void {
    merge(
      this.translate.onLangChange,
      this.translate.onTranslationChange,
      this.translate.onDefaultLangChange,
    )
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(() => this.updateDocumentTitle());

    const idParam = this.route.snapshot.paramMap.get('tenantId');
    if (idParam == null || idParam.trim() === '') {
      // Bare /loyalty — no tenant segment (do not show platform landing) — #373
      this.errorKind.set('missing_link');
      this.loading.set(false);
      this.updateDocumentTitle();
      return;
    }
    const id = Number(idParam);
    if (!Number.isFinite(id) || id <= 0) {
      this.errorKind.set('invalid_tenant');
      this.loading.set(false);
      this.updateDocumentTitle();
      return;
    }
    this.tenantId.set(id);
    const ref = this.route.snapshot.queryParamMap.get('ref');
    if (ref?.trim()) {
      this.referralCode = ref.trim();
    }
    forkJoin({
      program: this.api.getPublicLoyaltyProgram(id),
      tenant: this.api.getPublicTenant(id).pipe(
        tap((tenant) => {
          this.tenant.set(tenant);
          this.logoUrl.set(this.api.getTenantLogoUrl(tenant.logo_filename ?? undefined, tenant.id));
        }),
        catchError(() => of(null)),
      ),
    }).subscribe({
      next: ({ program, tenant }) => {
        this.program.set(program);
        if (tenant) {
          this.tenant.set(tenant);
          this.logoUrl.set(this.api.getTenantLogoUrl(tenant.logo_filename ?? undefined, tenant.id));
        }
        this.loading.set(false);
        this.updateDocumentTitle();
      },
      error: () => {
        // Program failed — still try tenant branding for the error shell.
        this.api.getPublicTenant(id).subscribe({
          next: (t) => {
            this.tenant.set(t);
            this.logoUrl.set(this.api.getTenantLogoUrl(t.logo_filename ?? undefined, t.id));
            this.errorKind.set('not_enabled');
            this.loading.set(false);
          },
          error: () => {
            this.errorKind.set('not_enabled');
            this.loading.set(false);
          },
        });
      },
    });
  }

  canSubmit(): boolean {
    if (!this.displayName.trim()) return false;
    const hasEmail = !!this.email.trim();
    const hasPhone = !!this.phone.trim();
    if (!hasEmail && !hasPhone) return false;
    if (hasEmail && !contactEmailValid(this.email)) return false;
    if (hasPhone && !contactPhoneValid(this.phone)) return false;
    return true;
  }

  canRecover(): boolean {
    const hasEmail = !!this.recoverEmail.trim();
    const hasPhone = !!this.recoverPhone.trim();
    if (!hasEmail && !hasPhone) return false;
    if (hasEmail && !contactEmailValid(this.recoverEmail)) return false;
    if (hasPhone && !contactPhoneValid(this.recoverPhone)) return false;
    return true;
  }

  private applyMembershipResult(res: {
    membership: {
      member_token?: string;
      balance: number;
      vip_tier?: string | null;
      referral_code?: string | null;
    };
    wallet?: {
      apple_wallet_available?: boolean;
      google_wallet_available?: boolean;
      apple_pkpass_path?: string | null;
      google_save_url?: string | null;
    };
    apple_pkpass_path?: string;
    google_save_url?: string;
  }): void {
    const token = res.membership.member_token ?? null;
    this.memberToken.set(token);
    this.balance.set(res.membership.balance);
    this.vipTier.set(res.membership.vip_tier ?? null);
    this.ownReferralCode.set(res.membership.referral_code ?? null);
    const applePath = res.apple_pkpass_path || res.wallet?.apple_pkpass_path;
    this.applePkpassUrl.set(
      token && (res.wallet?.apple_wallet_available || applePath)
        ? this.api.getPublicLoyaltyApplePkpassUrl(token)
        : null,
    );
    this.googleSaveUrl.set(
      res.wallet?.google_wallet_available
        ? res.google_save_url || res.wallet?.google_save_url || null
        : null,
    );
    this.submitted.set(true);
  }

  submit(): void {
    if (!this.canSubmit() || this.submitting()) return;
    this.submitting.set(true);
    this.submitError.set(null);
    this.recovered.set(false);
    const month = Number(this.birthdayMonth);
    const day = Number(this.birthdayDay);
    this.api
      .joinPublicLoyalty(this.tenantId(), {
        display_name: this.displayName.trim(),
        email: this.email.trim() || undefined,
        phone: this.phone.trim() || undefined,
        birthday_month: Number.isFinite(month) && month >= 1 ? month : null,
        birthday_day: Number.isFinite(day) && day >= 1 ? day : null,
        referral_code: this.referralCode.trim() || undefined,
      })
      .subscribe({
        next: (res) => {
          this.submitting.set(false);
          this.applyMembershipResult(res);
        },
        error: (err) => {
          this.submitting.set(false);
          this.submitError.set(err?.error?.detail || 'Join failed');
        },
      });
  }

  recover(): void {
    if (!this.canRecover() || this.recovering()) return;
    this.recovering.set(true);
    this.recoverError.set(null);
    this.api
      .recoverPublicLoyalty(this.tenantId(), {
        email: this.recoverEmail.trim() || undefined,
        phone: this.recoverPhone.trim() || undefined,
      })
      .subscribe({
        next: (res) => {
          this.recovering.set(false);
          this.recovered.set(true);
          this.applyMembershipResult(res);
        },
        error: (err) => {
          this.recovering.set(false);
          const detail = err?.error?.detail;
          this.recoverError.set(
            typeof detail === 'string' && detail
              ? detail
              : this.translate.instant('LOYALTY_PUBLIC.NOT_FOUND'),
          );
        },
      });
  }

  private updateDocumentTitle(): void {
    const name = this.program()?.program_name || this.translate.instant('LOYALTY_PUBLIC.TITLE');
    this.title.setTitle(String(name));
  }
}
