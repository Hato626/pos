import { Injectable } from '@angular/core';
import { Observable, EMPTY, catchError, concat, finalize, of, shareReplay, tap, throwError } from 'rxjs';
import type { TenantSummary } from './api.service';

/** Preserve public branding across guest tabs, scoped by the actual tenant and its slug. */
@Injectable({ providedIn: 'root' })
export class PublicTenantCacheService {
  private entries = new Map<string, { tenant: TenantSummary; refreshedAt: number }>();
  private pending = new Map<string, Observable<TenantSummary>>();
  private readonly maxAge = 60_000;

  get(ref: number | string, load: () => Observable<TenantSummary>): Observable<TenantSummary> {
    const key = String(ref).trim();
    const cached = this.entries.get(key);
    if (cached && Date.now() - cached.refreshedAt < this.maxAge) return of(cached.tenant);
    const canonical = cached ? String(cached.tenant.id) : key;
    let request = this.pending.get(canonical);
    if (!request) {
      request = load().pipe(
        tap((tenant) => {
          // Remove old aliases when a restaurant changes its public slug.
          for (const [alias, entry] of this.entries) {
            if (entry.tenant.id === tenant.id) this.entries.delete(alias);
          }
          const entry = { tenant, refreshedAt: Date.now() };
          for (const alias of [key, String(tenant.id), tenant.public_slug?.trim()]) {
            if (alias) this.entries.set(alias, entry);
          }
          // Bound an operator's long browsing session without persisting tenant data on disk.
          while (this.entries.size > 96) this.entries.delete(this.entries.keys().next().value!);
        }),
        catchError((error) => {
          if (cached && error.status !== 403 && error.status !== 404) return EMPTY;
          for (const [alias, entry] of this.entries) {
            if (alias === key || (cached && entry.tenant.id === cached.tenant.id)) this.entries.delete(alias);
          }
          return throwError(() => error);
        }),
        finalize(() => this.pending.delete(canonical)),
        shareReplay({ bufferSize: 1, refCount: false }),
      );
      this.pending.set(canonical, request);
    }
    // Refresh expired data without first reverting the page to its default theme.
    return cached ? concat(of(cached.tenant), request) : request;
  }
}
