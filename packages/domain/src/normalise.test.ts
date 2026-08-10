import { describe, expect, it } from 'vitest';
import { domainOfAddress, matchesDomainSuffix, normaliseCompany, normaliseRole } from './normalise.js';

describe('normaliseCompany', () => {
  it('strips legal suffixes so one company stays one company', () => {
    expect(normaliseCompany('Nexi S.p.A.')).toBe('nexi');
    expect(normaliseCompany('Prima Assicurazioni S.r.l.')).toBe('prima assicurazioni');
    expect(normaliseCompany('Zalando SE')).toBe('zalando se'); // SE is not in the list
    expect(normaliseCompany('Personio GmbH')).toBe('personio');
    expect(normaliseCompany('Bending Spoons  ')).toBe('bending spoons');
  });

  it('strips stacked suffixes', () => {
    expect(normaliseCompany('Foo Italia S.r.l.')).toBe('foo italia');
  });

  it('keeps "group" and "holding" — they are usually part of the real name', () => {
    expect(normaliseCompany('Iliad Group')).toBe('iliad group');
  });

  it('folds case and accents', () => {
    expect(normaliseCompany('Société Générale')).toBe(normaliseCompany('SOCIETE GENERALE'));
  });
});

describe('normaliseRole', () => {
  it('lifts seniority into its own field', () => {
    const r = normaliseRole('Senior Backend Engineer');
    expect(r.role).toBe('backend engineer');
    expect(r.seniority).toBe('senior');
  });

  it('expands the abbreviations the spec names', () => {
    expect(normaliseRole('Sr. BE Eng').role).toBe('backend engineer');
    expect(normaliseRole('Jr Dev').role).toBe('developer');
    expect(normaliseRole('SWE II').role).toBe('software engineer');
  });

  it('strips contract and diversity notation', () => {
    const r = normaliseRole('Backend Engineer (m/f/d)');
    expect(r.role).toBe('backend engineer');
  });

  it('strips a trailing location and keeps it', () => {
    const r = normaliseRole('Backend Engineer - Milan, full time');
    expect(r.role).toBe('backend engineer');
    expect(r.location).toBe('Milan');
  });

  it('detects work mode without letting it pollute the title', () => {
    const r = normaliseRole('Platform Engineer — Remote');
    expect(r.role).toBe('platform engineer');
    expect(r.workMode).toBe('remote');
  });

  it('two spellings of one job normalise to the same key', () => {
    expect(normaliseRole('Sr. Backend Engineer (f/m/d) – Berlin').role).toBe(
      normaliseRole('Senior Backend Engineer').role,
    );
  });

  it('does not eat a legitimate multi-part title', () => {
    expect(normaliseRole('Engineer, Payments').role).toBe('engineer payments');
  });
});

describe('domains', () => {
  it('reads the domain out of an address', () => {
    expect(domainOfAddress('Giulia <talent@nexi.it>')).toBe('nexi.it');
    expect(domainOfAddress('no-reply@eu.greenhouse-mail.io')).toBe('eu.greenhouse-mail.io');
    expect(domainOfAddress('not an address')).toBeNull();
  });

  it('matches vendor domains by suffix, not by substring', () => {
    expect(matchesDomainSuffix('eu.greenhouse-mail.io', 'greenhouse-mail.io')).toBe(true);
    expect(matchesDomainSuffix('greenhouse-mail.io', 'greenhouse-mail.io')).toBe(true);
    // The trap: a lookalike domain must not match.
    expect(matchesDomainSuffix('notgreenhouse-mail.io', 'greenhouse-mail.io')).toBe(false);
  });
});
