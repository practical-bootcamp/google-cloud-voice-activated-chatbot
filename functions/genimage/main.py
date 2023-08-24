
import os
from pathlib import Path

import requests
from flask import escape
import openai

import functions_framework
from google.cloud import storage

import smtplib
from email.mime.text import MIMEText
import urllib.parse
from google.cloud import datastore
import datetime
from datetime import timezone

openai.api_type = "azure"
openai.api_base = "https://eastus.api.cognitive.microsoft.com/"
openai.api_version = "2023-06-01-preview"
openai.api_key = os.getenv("OPENAI_API_KEY")

IMAGE_BUCKET = os.getenv("IMAGE_BUCKET")

@functions_framework.http
def genimage(request):
    """HTTP Cloud Function.
    Args:
        request (flask.Request): The request object.
        <https://flask.palletsprojects.com/en/1.1.x/api/#incoming-request-data>
    Returns:
        The response text, or any set of values that can be turned into a
        Response object using `make_response`
        <https://flask.palletsprojects.com/en/1.1.x/api/#flask.make_response>.
    """

    # Set CORS headers for the preflight request
    if request.method == "OPTIONS":
        # Allows GET requests from any origin with the Content-Type
        # header and caches preflight response for an 3600s
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, GET, PUT",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "3600",
        }

        return ("", 204, headers)
    headers = {"Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods":"POST, GET, PUT",
            "Access-Control-Allow-Headers": "Content-Type"}

    request_args = request.args

    key = request_args["key"]
    prompt = request_args["prompt"]
    email = request_args["email"]
    print(f"email: {email}")

    if key != os.getenv("SECRET_KEY"):
        return "Unauthorized", 401, headers
    
    if is_gen_image_job_exceed_rate_limit(email):
        return "Rate limit exceeded, and please wait for 30s!", 200, headers
    save_new_gen_image_job(email, prompt)
    response = openai.Image.create(
        prompt=prompt,
        size='1024x1024',
        n=1
    )
    image_url = response["data"][0]["url"]

    image_path =download_image(image_url)
    public_url = upload_image_to_bucket(image_path)

    params = {'key':key, 'email': email, 'public_url': public_url}
    approver_emails = os.getenv("APPROVER_EMAILS").split(",")
    subject = "Verify Gen Image for "+ email
    sender = os.getenv("GMAIL")
    recipients = approver_emails
    password = os.getenv("APP_PASSWORD")

    params = {'key':key, 'email': email, 'public_url': public_url}
    update_gen_image_job(email, public_url);
    send_email(subject, params, sender, recipients, password)   
                  
    return "Please check your email!", 200, headers
    
def download_image(image_url):   
    # Download image from url
    r = requests.get(image_url, allow_redirects=True)
    # Image name with hash or image_url
    image_name = "image-"+str(hash(image_url))+ ".png"
    image_path = f"/tmp/{image_name}"
    open(image_path, "wb").write(r.content)
    return image_path

def upload_image_to_bucket(image_path):
    # Upload image to bucket
    client = storage.Client()
    bucket = client.get_bucket(IMAGE_BUCKET)
    # extract image name from path
    image_name = Path(image_path).name
    blob = bucket.blob(image_name)
    blob.upload_from_filename(image_path)
    return blob.public_url
    
def send_email(subject:str, params:dict, sender:str, recipients:list[str], password:str):
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp_server:
        smtp_server.login(sender, password)   
        for recipient in recipients:            
            params['approver_email'] = recipient
            body = get_email_body(params)
            msg = MIMEText(body)
            msg['Subject'] = subject
            msg['From'] = sender
            msg['To'] = recipient
            smtp_server.sendmail(sender, recipient, msg.as_string())
     
    print("Message sent!")

def get_email_body(params:dict) -> str:    
    approval_url = os.getenv("APPROVAL_URL")+"?" + urllib.parse.urlencode(params, doseq=True)
    body= f"""
    Please check the following image and click the link to approve it.

    {params["public_url"]}

    Approve: 

    {approval_url}

    """
    return body

def save_new_gen_image_job(email: str, prompt:str) -> bool:
    client = datastore.Client(project=os.environ.get('GCP_PROJECT'))
    key = client.key('GenImageJob', email)
    entity = datastore.Entity(key=key)
    now = datetime.datetime.now(timezone.utc);
    entity.update({
        'email': email,
        'prompt': prompt ,      
        'status': "GENERATING_IMAGE",
        'create_time': now,
        'modify_time': now 
    })
    client.put(entity)

def update_gen_image_job(email: str, image_url:str) -> bool:
    client = datastore.Client(project=os.environ.get('GCP_PROJECT'))
    with client.transaction():
        old_key = client.key('GenImageJob', email) 
        old_entity= client.get(old_key)       
        entity = datastore.Entity(key=client.key('GenImageJob', email + "->" +image_url))
        entity.update({
        'email': email,
        'prompt': old_entity['prompt'] ,      
        'status': "WAITING_FOR_APPROVAL",
        'create_time': old_entity['create_time'],
        'modify_time': datetime.datetime.now(timezone.utc) 
        })     
        client.put(entity)
        client.delete(old_key)     


def is_gen_image_job_exceed_rate_limit(email: str) -> bool:
    client = datastore.Client(project=os.environ.get('GCP_PROJECT'))
    query = client.query(kind='GenImageJob')
    query.add_filter('email', '=', email)    
    query.order = ['-modify_time']
    results = list(query.fetch(limit=1))
    if len(results) == 0:
        return False
    else:
        last_approved_time = results[0]['modify_time']
        now = datetime.datetime.now(timezone.utc)
        diff = now - last_approved_time
        waiting_time = 60 / int(os.environ.get('RATE_LIMIT_PER_MINUTE'))
        return diff.seconds < waiting_time  # 30 seconds rate limit
    
