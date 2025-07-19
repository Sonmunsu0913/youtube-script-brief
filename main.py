from flask import Flask, request, jsonify
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
from youtube_transcript_api.proxies import WebshareProxyConfig
import openai
import re
import random

import requests
from youtube_transcript_api._api import YouTubeTranscriptApi as BaseYouTubeTranscriptApi  # 추가

import logging
import sys

# 가장 상단에 추가
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
      logging.FileHandler("app.log", encoding='utf-8'),  # 파일로 로그 저장
      logging.StreamHandler(sys.stdout)                  # 콘솔에도 출력
    ]
)

logger = logging.getLogger(__name__)

# Flask 앱 생성
app = Flask(__name__)

################################## 사용자 설정 #####################################

# openai.api_key = '' # 챗GPT API 키 입력
MODEL = 'gpt-4-turbo' # 챗GPT 모델설정 'gpt-3.5-turbo' OR 'gpt-4'
TEMPERATURE = 0.5 # 결과물 창의성의 정도/ 0~1까지 설정가능(1에 가까울수록 창의적인 결과값으로 추출됨)

limit_text_num = 300 # 요약본 결과물 글자수 제한
hashtag_min_cnt = 10 # 추출할 해시태그의의 최소갯수
hashtag_max_cnt = 10 # 추출할 해시태그의의 최대갯수

# Webshare 프록시 설정 (Smartproxy에서도 Webshare 형식이 동일하게 적용 가능)
PROXY_USERNAME = ""
PROXY_PASSWORD = ""

ytt_api = YouTubeTranscriptApi(
    proxy_config=WebshareProxyConfig(
        proxy_username=PROXY_USERNAME,
        proxy_password=PROXY_PASSWORD,
    )
)

# proxies = {
#     "http": "http://spif6oghfk:FusWu8JphO6i79wu~z@gate.smartproxy.com:10004",  # HTTP 프록시
#     "https": "https://spif6oghfk:FusWu8JphO6i79wu~z@gate.smartproxy.com:10004",  # HTTPS 프록시
# }

###################################################################################

#유튜브 URL에서 video_id 추출 함수
def extract_video_id(youtube_url):
  match = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", youtube_url)
  if match:
    return match.group(1)
  raise ValueError("Invalid YouTube URL")

def get_transcript(video_id):
  try:
    logger.info(f"▶ get_transcript() - video_id: {video_id}")

    # 현재 IP 로그 확인용 요청
    try:
      ip_check = requests.get("https://httpbin.org/ip", timeout=5)
      logger.info(f"🌐 현재 서버 IP (프록시 안 씀): {ip_check.text.strip()}")
    except Exception as ip_err:
      logger.warning(f"⚠️ 현재 IP 확인 실패: {ip_err}")

    # 유튜브 자막 리스트 가져오기
    # transcript_list = ytt_api.list_transcripts(video_id)
    # transcript_list = ytt_api.fetch(video_id)
    transcript_list = ytt_api.list(video_id)
    transcript = transcript_list.find_transcript(["ko", "ko-KR", "a.ko", "ko.auto"]).fetch()

    logger.info(f"✅ transcript 추출 성공, 길이: {len(transcript)}")
    return transcript

  except TranscriptsDisabled:
    logger.info("⚠️ 자막이 비활성화된 영상입니다.")
    return {'error': 'Subtitles are disabled for this video.'}
  except NoTranscriptFound:
    logger.info("⚠️ 자막을 찾을 수 없습니다.")
    return {'error': 'No transcript found for the video.'}
  except Exception as e:
    logger.info(f"❌ 기타 오류 발생: {e}")
    return {'error': str(e)}

def merge_transcript(transcript):
  return " ".join([item.text for item in transcript])


# 요약 및 해시태그 생성 함수
def summarize_text(text, main_keywords):
  # 요약 요청 프롬프트
  main_keyword = ', '.join([f"'{keyword}'" for keyword in main_keywords])  # 메인키워드 문자열 변환
  summary_prompt = f"이 스크립트는 주식전문가가 {main_keyword} 종목을 추천한 내용입니다. {main_keyword} 해당 종목에 왜 추천해줬는지 줄바꿈 없이 요약해주세요. :"
  hashtag_prompt = f"다음 요약을 바탕으로 {hashtag_max_cnt}개의 관련 해시태그를 문자열로 줄바꿈 없이 한줄로 생성해 주세요. 해시태그는 {main_keyword}에 초점을 맞추어 한국어로 생성하되, 중요한 순으로 {hashtag_max_cnt}개를 뽑아주세요.:"

  # 요약 생성
  response = openai.chat.completions.create(
      model=MODEL,
      messages=[
        {"role": "system", "content": "You are a helpful assistant that summarizes text in Korean."},
        {"role": "user", "content": f"{summary_prompt}\n\n{text}"}
      ],
      max_tokens=1000,
      temperature=TEMPERATURE
  )
  summary = response.choices[0].message.content
  summary_tokens = response.usage.total_tokens

  # 해시태그 생성
  hashtag_response = openai.chat.completions.create(
      model=MODEL,
      messages=[
        {"role": "system", "content": "You are a helpful assistant that generates hashtags in Korean."},
        {"role": "user", "content": f"{hashtag_prompt}\n\n{summary}"}
      ],
      max_tokens=150,
      temperature=TEMPERATURE
  )
  hashtags_text = hashtag_response.choices[0].message.content
  hashtags = [tag.strip() for tag in hashtags_text.split() if tag.strip().startswith('#')]
  hashtag_tokens = hashtag_response.usage.total_tokens

  return summary, hashtags, summary_tokens, hashtag_tokens, summary_tokens + hashtag_tokens

# Flask API 엔드포인트
@app.route('/youtube/script/brief', methods=['POST'])
def process_youtube_video():
  try:
    # 요청 데이터 가져오기
    data = request.json
    youtube_url = data.get('youtube_url')
    main_keywords = data.get('main_keywords')

    if not youtube_url or not main_keywords:
      return jsonify({"error": "youtube_url and main_keywords are required"}), 400

    logger.info(f"▶ 요청된 URL: {youtube_url}")
    logger.info(f"▶ 요청된 키워드: {main_keywords}")

    # video_id 추출 및 자막 요청
    video_id = extract_video_id(youtube_url)
    transcript = get_transcript(video_id)

    if 'error' in transcript:
      return jsonify({"error": transcript['error']}), 400

    # 자막 병합 후 요약/해시태그 생성
    transcript_text = merge_transcript(transcript)
    summary, hashtags, summary_tokens, hashtag_tokens, total_tokens = summarize_text(transcript_text, main_keywords)

    logger.info(f"▶ 요약 결과: {summary}")
    logger.info(f"▶ 해시태그 결과: {hashtags}")

    # 결과 반환
    return jsonify({
      "summary": summary,
      "hashtags": hashtags
    }), 200

  except ValueError as e:
    return jsonify({"error": str(e)}), 400
  except Exception as e:
    return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

# 서버 실행
if __name__ == '__main__':
  app.run(host='0.0.0.0', port=8080)


