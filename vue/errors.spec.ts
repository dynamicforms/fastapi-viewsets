import { interpolate } from '@dynamicforms/translatable';

import { translateApiError, translateStrings } from './errors';

const untranslated = (key: string) => key;

describe('translateApiError', () => {
  afterEach(() => {
    translateStrings(untranslated);
  });

  it('should return detail unchanged when detail_code is absent', () => {
    const result = translateApiError({ detail: 'Not your item' });
    expect(result).toBe('Not your item');
  });

  it('should interpolate the English default for a known code', () => {
    const result = translateApiError({
      detail: 'Item with pk 5 not found',
      detail_code: 'not_found',
      detail_params: { pk: 5 },
    });
    expect(result).toBe('Item with pk 5 not found');
  });

  it('should join an array param for unsupported_list_shape', () => {
    const result = translateApiError({
      detail: 'unsupported list shape "cursor"; this endpoint offers plain, paginated',
      detail_code: 'unsupported_list_shape',
      detail_params: { shape: 'cursor', allowed: ['plain', 'paginated'] },
    });
    expect(result).toBe('unsupported list shape "cursor"; this endpoint offers plain, paginated');
  });

  it('should join an array param for cursor_missing_keys', () => {
    const result = translateApiError({
      detail: 'cursor has no value for ordering key(s): id, year',
      detail_code: 'cursor_missing_keys',
      detail_params: { missing: ['id', 'year'] },
    });
    expect(result).toBe('cursor has no value for ordering key(s): id, year');
  });

  it('should fall back to detail for an unrecognized code', () => {
    const result = translateApiError({ detail: 'Something new', detail_code: 'something_new' });
    expect(result).toBe('Something new');
  });

  it('should reflect a later translateStrings call', () => {
    const translations: Record<string, string> = { 'errors.not_found': 'Vnos s ključem {pk} ne obstaja' };
    translateStrings((key, named) => (key in translations ? interpolate(translations[key], named) : key), 'errors');

    const result = translateApiError({
      detail: 'Item with pk 5 not found',
      detail_code: 'not_found',
      detail_params: { pk: 5 },
    });
    expect(result).toBe('Vnos s ključem 5 ne obstaja');
  });

  it('should translate a code the table does not declare', () => {
    translateStrings((key) => (key === 'insufficient_balance' ? 'Stanje ne zadošča' : key));

    const result = translateApiError({ detail: 'balance is short', detail_code: 'insufficient_balance' });
    expect(result).toBe('Stanje ne zadošča');
  });

  it('should pass a joined array param to the translation function', () => {
    const seen: Record<string, unknown>[] = [];
    translateStrings((key, named) => {
      seen.push(named);
      return key;
    });

    translateApiError({
      detail: 'cursor has no value for ordering key(s): id, year',
      detail_code: 'cursor_missing_keys',
      detail_params: { missing: ['id', 'year'] },
    });
    expect(seen).toEqual([{ missing: 'id, year' }]);
  });
});
