import { isApiErrorDetail, translatableStrings, translateApiError, translateStrings } from './errors';

describe('isApiErrorDetail', () => {
  it('should recognize a structured error detail', () => {
    expect(isApiErrorDetail({ message: 'x', code: 'not_found', params: {} })).toBe(true);
  });

  it('should reject a plain string detail', () => {
    expect(isApiErrorDetail('Not your item')).toBe(false);
  });

  it('should reject null and non-object values', () => {
    expect(isApiErrorDetail(null)).toBe(false);
    expect(isApiErrorDetail(42)).toBe(false);
  });
});

describe('translateApiError', () => {
  afterEach(() => {
    translateStrings(() => undefined);
  });

  it('should interpolate the English default for a known code', () => {
    const result = translateApiError({ message: 'Item with pk 5 not found', code: 'not_found', params: { pk: 5 } });
    expect(result).toBe('Item with pk 5 not found');
  });

  it('should join an array param for unsupported_list_shape', () => {
    const result = translateApiError({
      message: 'unsupported list shape "cursor"; this endpoint offers plain, paginated',
      code: 'unsupported_list_shape',
      params: { shape: 'cursor', allowed: ['plain', 'paginated'] },
    });
    expect(result).toBe('unsupported list shape "cursor"; this endpoint offers plain, paginated');
  });

  it('should join an array param for cursor_missing_keys', () => {
    const result = translateApiError({
      message: 'cursor has no value for ordering key(s): id, year',
      code: 'cursor_missing_keys',
      params: { missing: ['id', 'year'] },
    });
    expect(result).toBe('cursor has no value for ordering key(s): id, year');
  });

  it('should fall back to the server message for an unrecognized code', () => {
    const result = translateApiError({ message: 'Something new', code: 'something_new', params: {} });
    expect(result).toBe('Something new');
  });

  it('should reflect a later translateStrings call', () => {
    const translations: Partial<Record<keyof typeof translatableStrings, string>> = {
      not_found: 'Vnos s ključem {pk} ne obstaja',
    };
    translateStrings((key) => translations[key]);

    const result = translateApiError({ message: 'Item with pk 5 not found', code: 'not_found', params: { pk: 5 } });
    expect(result).toBe('Vnos s ključem 5 ne obstaja');
  });
});
