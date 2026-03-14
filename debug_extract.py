from app import app

if __name__ == '__main__':
    client = app.test_client()
    resp = client.get('/extract', query_string={'url': 'https://youtu.be/qZSj3j2bsPo?si=zKiK_fgC_H1jLn1U'})
    print('status', resp.status_code)
    print(resp.get_data(as_text=True))
