def validate_token(token):
    if not isinstance(token, str) or not 1 <= len(token) <= 2048 or any(not 33 <= ord(c) <= 126 for c in token):
        raise ValueError('请输入有效的 GitHub Token，不能包含空格或换行。')
    return token
