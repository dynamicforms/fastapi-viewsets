import {
  FIELD_ERROR_CODES,
  FIELD_ERROR_PARAM_NAMES,
  type FieldErrorCode,
  type FieldErrorOf,
  type FieldErrorParams,
  isFieldErrorCode,
} from './field-error-codes';
import listed from './field-error-codes.json';

import * as entry from './index';

// One value per code: `satisfies` fails to compile on an undeclared param and on a missing declared one.
const SAMPLE = {
  'fastapi-viewsets.required': {},
  'fastapi-viewsets.min_length': { minLength: 3 },
  'fastapi-viewsets.max_length': { maxLength: 3 },
  'fastapi-viewsets.min_value': { minValue: 1 },
  'fastapi-viewsets.min_value_exclusive': { minValue: '2000-01-01' },
  'fastapi-viewsets.max_value': { maxValue: 1 },
  'fastapi-viewsets.max_value_exclusive': { maxValue: 1.5 },
  'fastapi-viewsets.multiple_of': { multipleOf: 5 },
  'fastapi-viewsets.max_digits': { maxDigits: 3 },
  'fastapi-viewsets.max_decimal_places': { maxDecimalPlaces: 1 },
  'fastapi-viewsets.max_whole_digits': { maxWholeDigits: 2 },
  'fastapi-viewsets.pattern': { pattern: '^[a-z]+$' },
  'fastapi-viewsets.in_allowed_values': { allowedAsText: "'s' or 'm'" },
  'fastapi-viewsets.invalid_choice': {},
  'fastapi-viewsets.invalid_integer': {},
  'fastapi-viewsets.invalid_number': {},
  'fastapi-viewsets.invalid_boolean': {},
  'fastapi-viewsets.invalid_date': {},
  'fastapi-viewsets.invalid_time': {},
  'fastapi-viewsets.invalid_datetime': {},
  'fastapi-viewsets.invalid_duration': {},
  'fastapi-viewsets.invalid_url': {},
  'fastapi-viewsets.invalid_uuid': {},
  'fastapi-viewsets.past': {},
  'fastapi-viewsets.future': {},
  'fastapi-viewsets.invalid_type': {},
  'fastapi-viewsets.unique': { fields: ['title'] },
  'fastapi-viewsets.invalid': {},
  'fastapi-viewsets.pk_assigned_by_server': {},
} satisfies { [C in FieldErrorCode]: FieldErrorParams[C] };

describe('field error codes', () => {
  it('should list the codes and params of the Python package, in its order', () => {
    expect(FIELD_ERROR_PARAM_NAMES).toEqual(listed);
    expect(Object.keys(FIELD_ERROR_PARAM_NAMES)).toEqual(Object.keys(listed));
    expect(FIELD_ERROR_CODES).toEqual(Object.keys(listed));
  });

  it('should declare in the params interface exactly the params of the list', () => {
    expect(Object.keys(SAMPLE)).toEqual(Object.keys(listed));
    for (const [code, params] of Object.entries(listed)) {
      expect(Object.keys(SAMPLE[code as FieldErrorCode])).toEqual(params);
    }
  });

  it('should tell a declared code from any other', () => {
    expect(isFieldErrorCode('fastapi-viewsets.min_length')).toBe(true);
    expect(isFieldErrorCode('pydantic.json_invalid')).toBe(false);
    expect(isFieldErrorCode('toString')).toBe(false);
  });

  it('should narrow a field error by its code', () => {
    const error: FieldErrorOf<'fastapi-viewsets.min_length'> = {
      code: 'fastapi-viewsets.min_length',
      params: { minLength: 3 },
      detail: 'String should have at least 3 characters',
    };
    expect(error.params.minLength).toBe(3);
  });

  it('should be exported from the package root', () => {
    expect(entry.FIELD_ERROR_CODES).toBe(FIELD_ERROR_CODES);
    expect(entry.FIELD_ERROR_PARAM_NAMES).toBe(FIELD_ERROR_PARAM_NAMES);
    expect(entry.isFieldErrorCode).toBe(isFieldErrorCode);
    expect(entry.ERROR_NAMESPACE_DJANGO).toBe('django');
  });
});
