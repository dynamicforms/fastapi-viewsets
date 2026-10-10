import { toErrorDescription, toFieldErrors } from './errors';

describe('toErrorDescription', () => {
  it('should take the code, params and detail of a coded body, with origin server', () => {
    const error = toErrorDescription({
      detail: 'Item with pk 5 not found',
      detail_code: 'not_found',
      detail_params: { pk: 5 },
    });

    expect(error).toEqual({
      code: 'not_found',
      params: { pk: 5 },
      detail: 'Item with pk 5 not found',
      origin: 'server',
    });
  });

  it('should give a body without a code an empty code and no params', () => {
    expect(toErrorDescription({ detail: 'Not your item' })).toEqual({
      code: '',
      params: {},
      detail: 'Not your item',
      origin: 'server',
    });
  });

  it('should keep params as the server sent them', () => {
    const error = toErrorDescription({
      detail: 'unsupported list shape "paged"; this endpoint offers cursor, flat',
      detail_code: 'unsupported_list_shape',
      detail_params: { shape: 'paged', allowed: ['cursor', 'flat'] },
    });

    expect(error.params).toEqual({ shape: 'paged', allowed: ['cursor', 'flat'] });
  });
});

describe('toFieldErrors', () => {
  it('should key each failure by its location without the source, joined with dots', () => {
    const errors = toFieldErrors({
      detail: [
        {
          type: 'string_too_short',
          loc: ['body', 'name'],
          msg: 'String should have at least 3 characters',
          ctx: { min_length: 3 },
        },
        { type: 'int_parsing', loc: ['body', 'age'], msg: 'Input should be a valid integer' },
        {
          type: 'string_too_short',
          loc: ['body', 'address', 'city'],
          msg: 'String should have at least 3 characters',
          ctx: { min_length: 3 },
        },
      ],
    });

    expect(errors).toEqual({
      name: [
        {
          code: 'string_too_short',
          params: { min_length: 3 },
          detail: 'String should have at least 3 characters',
          origin: 'server',
        },
      ],
      age: [{ code: 'int_parsing', params: {}, detail: 'Input should be a valid integer', origin: 'server' }],
      'address.city': [
        {
          code: 'string_too_short',
          params: { min_length: 3 },
          detail: 'String should have at least 3 characters',
          origin: 'server',
        },
      ],
    });
  });

  it('should collect several failures of one field, and key a failure of the whole body as an empty name', () => {
    const errors = toFieldErrors({
      detail: [
        { type: 'missing', loc: ['body'], msg: 'Field required' },
        { type: 'a', loc: ['body', 'items', 0], msg: 'first' },
        { type: 'b', loc: ['body', 'items', 0], msg: 'second' },
      ],
    });

    expect(Object.keys(errors)).toEqual(['', 'items.0']);
    expect(errors['items.0'].map((e) => e.code)).toEqual(['a', 'b']);
  });
});
