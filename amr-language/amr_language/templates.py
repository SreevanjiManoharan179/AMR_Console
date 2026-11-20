"""Fixed user-facing messages. Tamil: needs native-speaker check.

Malayalam/Telugu deliberately use an explicitly labelled English fallback until
native-speaker-reviewed templates exist. Model-generated reason is never shown.
"""
MESSAGES = {
    'invalid_input': ('Give one request of 1–300 characters.', '1–300 எழுத்துகளில் ஒரு கோரிக்கையைக் கொடுக்கவும்.'),
    'invalid_names': ('Approved names are unavailable or invalid.', 'அங்கீகரிக்கப்பட்ட பெயர்கள் கிடைக்கவில்லை அல்லது செல்லுபடியாகவில்லை.'),
    'model_unavailable': ('The local language model is unavailable.', 'உள்ளூர் மொழி மாதிரி கிடைக்கவில்லை.'),
    'model_timeout': ('The local language model took too long.', 'உள்ளூர் மொழி மாதிரி அதிக நேரம் எடுத்துக்கொண்டது.'),
    'model_output_invalid': ('The local model returned an invalid task form.', 'உள்ளூர் மாதிரி செல்லுபடியாகாத பணி வடிவத்தை வழங்கியது.'),
    'out_of_scope': ('Give a supported positive task without conditions.', 'நிபந்தனைகள் இல்லாத ஆதரிக்கப்படும் நேர்மறையான பணியைக் கொடுக்கவும்.'),
    'inconsistent_plan': ('Please give one clear task; the model samples disagree.', 'ஒரு தெளிவான பணியைக் கொடுக்கவும்; மாதிரியின் பதில்கள் வேறுபடுகின்றன.'),
    'unknown_name': ('Please use a name from the approved list.', 'அங்கீகரிக்கப்பட்ட பட்டியலில் உள்ள பெயரைப் பயன்படுத்தவும்.'),
    'uncalibrated': ('Real-command calibration is required before accepting model tasks.', 'மாதிரிப் பணிகளை ஏற்கும் முன் உண்மையான கட்டளைகளைக் கொண்டு அளவுத்திருத்தம் தேவை.'),
    'conflicting_targets': ('The approved targets do not identify one destination. Please give one destination.', 'அங்கீகரிக்கப்பட்ட இலக்குகள் ஒரே இடத்தைக் குறிக்கவில்லை. ஒரு இலக்கைக் கொடுக்கவும்.'),
    'invalid_settings': ('The language module settings are invalid.', 'மொழித் தொகுதி அமைப்புகள் செல்லுபடியாகவில்லை.'),
}


def reply_language(language):
    return 'ta' if language == 'ta' else 'en'


def refusal(template, language):
    lang = reply_language(language)
    return {'template': template, 'language': lang, 'text': MESSAGES[template][lang == 'ta']}


def question(options, language):
    lang = reply_language(language)
    # options are approved IDs only, never raw model text.
    labels = ', '.join(key.replace('_', ' ') for key in options)
    text = f'Which one: {labels}?' if lang == 'en' else f'எது: {labels}?'
    return {'template': 'which_one', 'language': lang, 'options': options, 'text': text}
