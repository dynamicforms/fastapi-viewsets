/**
 * The field error codes the BE declares, with the params of each. A field error whose `code` is
 * not one of them is `pydantic.<type>` with pydantic's `ctx`, `django.<code>` with Django's params, an
 * application's namespaced code with its own params, or `pydantic.custom_error` with `{ type, ctx }`.
 */
export interface FieldErrorParams {
  /** No value given, or null where null is not allowed. */
  'fastapi-viewsets.required': Record<string, never>;
  /** Too few characters, items or bytes. */
  'fastapi-viewsets.min_length': { minLength: number };
  /** Too many characters, items or bytes. */
  'fastapi-viewsets.max_length': { maxLength: number };
  /** Below an inclusive lower bound; a date or time bound is an ISO string. */
  'fastapi-viewsets.min_value': { minValue: number | string };
  /** Not greater than an exclusive lower bound; a date or time bound is an ISO string. */
  'fastapi-viewsets.min_value_exclusive': { minValue: number | string };
  /** Above an inclusive upper bound; a date or time bound is an ISO string. */
  'fastapi-viewsets.max_value': { maxValue: number | string };
  /** Not less than an exclusive upper bound; a date or time bound is an ISO string. */
  'fastapi-viewsets.max_value_exclusive': { maxValue: number | string };
  /** Not a multiple of `multipleOf`. */
  'fastapi-viewsets.multiple_of': { multipleOf: number };
  /** Too many digits in total. */
  'fastapi-viewsets.max_digits': { maxDigits: number };
  /** Too many decimal places. */
  'fastapi-viewsets.max_decimal_places': { maxDecimalPlaces: number };
  /** Too many digits before the decimal point. */
  'fastapi-viewsets.max_whole_digits': { maxWholeDigits: number };
  /** Does not match the regular expression; `pattern` is its source. */
  'fastapi-viewsets.pattern': { pattern: string };
  /** Not one of the allowed values; `allowedAsText` is pydantic's text of them, e.g. `'a', 'b' or 'c'`. */
  'fastapi-viewsets.in_allowed_values': { allowedAsText: string };
  /** Not an allowed value; the allowed values are not in the params. */
  'fastapi-viewsets.invalid_choice': Record<string, never>;
  /** Not an integer. */
  'fastapi-viewsets.invalid_integer': Record<string, never>;
  /** Not a number. */
  'fastapi-viewsets.invalid_number': Record<string, never>;
  /** Not a boolean. */
  'fastapi-viewsets.invalid_boolean': Record<string, never>;
  /** Not a date. */
  'fastapi-viewsets.invalid_date': Record<string, never>;
  /** Not a time. */
  'fastapi-viewsets.invalid_time': Record<string, never>;
  /** Not a date and time. */
  'fastapi-viewsets.invalid_datetime': Record<string, never>;
  /** Not a duration. */
  'fastapi-viewsets.invalid_duration': Record<string, never>;
  /** Not a URL, or a URL with invalid syntax. */
  'fastapi-viewsets.invalid_url': Record<string, never>;
  /** Not a UUID. */
  'fastapi-viewsets.invalid_uuid': Record<string, never>;
  /** A date or date and time not in the past. */
  'fastapi-viewsets.past': Record<string, never>;
  /** A date or date and time not in the future. */
  'fastapi-viewsets.future': Record<string, never>;
  /** The value has the wrong JSON or structural type. */
  'fastapi-viewsets.invalid_type': Record<string, never>;
  /** A row with these values exists; `fields` are the Django model field names. */
  'fastapi-viewsets.unique': { fields: string[] };
  /** Not valid; the reason is in `detail`. */
  'fastapi-viewsets.invalid': Record<string, never>;
  /** A primary key in a create's body. */
  'fastapi-viewsets.pk_assigned_by_server': Record<string, never>;
}

/** A field error code the BE declares. */
export type FieldErrorCode = keyof FieldErrorParams;

/** A field error with code `C` and its params. */
export interface FieldErrorOf<C extends FieldErrorCode> {
  code: C;
  params: FieldErrorParams[C];
  detail: string;
}

/** The names of the params of each field error code, in the order the BE lists them. */
export const FIELD_ERROR_PARAM_NAMES: {
  readonly [C in FieldErrorCode]: readonly (keyof FieldErrorParams[C] & string)[];
} = {
  'fastapi-viewsets.required': [],
  'fastapi-viewsets.min_length': ['minLength'],
  'fastapi-viewsets.max_length': ['maxLength'],
  'fastapi-viewsets.min_value': ['minValue'],
  'fastapi-viewsets.min_value_exclusive': ['minValue'],
  'fastapi-viewsets.max_value': ['maxValue'],
  'fastapi-viewsets.max_value_exclusive': ['maxValue'],
  'fastapi-viewsets.multiple_of': ['multipleOf'],
  'fastapi-viewsets.max_digits': ['maxDigits'],
  'fastapi-viewsets.max_decimal_places': ['maxDecimalPlaces'],
  'fastapi-viewsets.max_whole_digits': ['maxWholeDigits'],
  'fastapi-viewsets.pattern': ['pattern'],
  'fastapi-viewsets.in_allowed_values': ['allowedAsText'],
  'fastapi-viewsets.invalid_choice': [],
  'fastapi-viewsets.invalid_integer': [],
  'fastapi-viewsets.invalid_number': [],
  'fastapi-viewsets.invalid_boolean': [],
  'fastapi-viewsets.invalid_date': [],
  'fastapi-viewsets.invalid_time': [],
  'fastapi-viewsets.invalid_datetime': [],
  'fastapi-viewsets.invalid_duration': [],
  'fastapi-viewsets.invalid_url': [],
  'fastapi-viewsets.invalid_uuid': [],
  'fastapi-viewsets.past': [],
  'fastapi-viewsets.future': [],
  'fastapi-viewsets.invalid_type': [],
  'fastapi-viewsets.unique': ['fields'],
  'fastapi-viewsets.invalid': [],
  'fastapi-viewsets.pk_assigned_by_server': [],
};

/** Every field error code the BE declares. */
export const FIELD_ERROR_CODES = Object.keys(FIELD_ERROR_PARAM_NAMES) as readonly FieldErrorCode[];

/** Whether `code` is a field error code the BE declares. */
export function isFieldErrorCode(code: string): code is FieldErrorCode {
  return Object.prototype.hasOwnProperty.call(FIELD_ERROR_PARAM_NAMES, code);
}
