## Security Note:
* Always use HTTPS.
* Avoid hardcoding credentials in production.
* For sensitive use cases, consider using OAuth 2 instead of Basic Auth.

---

## Steps to Implement OAuth 2
Install Required Module  
Use Composer to install the module:

```sh
composer require drupal/simple_oauth
drush en simple_oauth
```

Or for OAuth2 Server:

```sh
composer require drupal/oauth2_server
drush en oauth2_server
```

## Generate Public/Private Keys
Create RSA keys and store them outside the web root. Configure the paths at `/admin/config/people/simple_oauth`.
 
## Create OAuth 2 Client
Go to `/admin/config/services/consumer` (Simple OAuth) or `/admin/config/people/oauth2-server` (OAuth2 Server). Add a client with:

## Client ID and secret
Grant type (e.g., client_credentials, password)
Scopes tied to user permissions 

## Configure Permissions
Assign roles and permissions to control access to REST/JSON:API endpoints. Ensure routes require _permission: 'use oauth2 token'. 

## Obtain Access Token
Request a token via POST to `/oauth/token`:

```
POST /oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=CLIENT_ID&client_secret=CLIENT_SECRET
```

## Use Token in API Requests
Include the Bearer token in the Authorization header:

```
GET /jsonapi/node/article
Authorization: Bearer <access_token>
```

<br>
