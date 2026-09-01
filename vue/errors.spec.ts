import { translatableStrings, translateApiError, translateStrings } from './errors';

describe('translateApiError', () => {
  afterEach(() => {
    translateStrings(() => undefined);
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
    const translations: Partial<Record<keyof typeof translatableStrings, string>> = {
      not_found: 'Vnos s ključem {pk} ne obstaja',
    };
    translateStrings((key) => translations[key]);

    const result = translateApiError({
      detail: 'Item with pk 5 not found',
      detail_code: 'not_found',
      detail_params: { pk: 5 },
    });
    expect(result).toBe('Vnos s ključem 5 ne obstaja');
  });
});
