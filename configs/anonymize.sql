-- Snaplicator anonymization script
-- This file runs inside the clone's Postgres container on every clone creation.
-- Use ON_ERROR_STOP to abort on any error.
\set ON_ERROR_STOP on

-- Example: obfuscate emails on a demo users table (adjust to your schema)
-- UPDATE users SET email = concat('user+', id, '@example.local');

-- Place your anonymization SQL below.

-- set password to very strong one
update "user" set "encrypted_password" = '$2b$12$VWnmXwkblB96B5EvyE7LtO1shYPNqsWWWo1IxYFYAWft/Qt6k6omy';
-- asdf1234!
update "auth_userpassword" set "password" = '$2a$12$cR7ABtNInV5UxTNk2zK2Jeo0i3LfyThJrTslnOuiKv0tGEVJNk9Km';
-- set push token to brandazine's
update "curator_deviceinfo" set "push_token" = 'fh07_3qTF0PAoM8kPl9JEv:APA91bFH-gMQg6OJTKleCIFiU1bAZ2SZgd2HLziJFxHCbWw12glFMjt9i2SDzEn2C4eKifDZbfq4IgQ8AdOSzL6_oiGAwWDSrXZUKEdzvKdVrQyKqt577iuRfjaokJHkQbvHiW9EcUx9', "is_push_notification_enabled" = false;

update "user_usercard" set "iamport_data" = '{
  "pg_id": "nictest04m",
  "updated": 1648051343,
  "inserted": 1648051343,
  "card_code": "374",
  "card_name": "하나SK카드",
  "card_type": 0,
  "card_number": "51818500****3264",
  "pg_provider": "nice",
  "customer_tel": null,
  "customer_uid": "BC_3774_555908",
  "customer_addr": null,
  "customer_name": null,
  "customer_email": null,
  "customer_postcode": null
}', "customer_uid" = 'BC_3770_831054';
update "brand_brandcard" set "iamport_data" = '{
  "pg_id": "nictest04m",
  "updated": 1648051343,
  "inserted": 1648051343,
  "card_code": "374",
  "card_name": "하나SK카드",
  "card_type": 0,
  "card_number": "51818500****3264",
  "pg_provider": "nice",
  "customer_tel": null,
  "customer_uid": "BC_3774_555908",
  "customer_addr": null,
  "customer_name": null,
  "customer_email": null,
  "customer_postcode": null
}', "customer_uid" = 'unencrypted::BC_3770_831054';

update "brand_brand" set "business_email" = 'dev+' || split_part("business_email", '@', 1) || '@brandazine.kr', "out_stock_postcode" = '04514', "out_stock_address_1" = '서울 중구 서소문로 120 (서소문동, ENA Center', "out_stock_address_2" = '6층';
update "brand_brandadmin" set "password" = '$2b$12$VWnmXwkblB96B5EvyE7LtO1shYPNqsWWWo1IxYFYAWft/Qt6k6omy', "phone" = '+821031351310';
--update "recurrence" set "is_enabled" = false;

update "tryset_v2_tryset" set "phone" = '+821031351310', "email" = 'dev@brandazine.kr', "destination_phone" = '+821031351310';
update "order_v2_invoice" set "phone" = '+821031351310', "email" = 'dev@brandazine.kr', "imp_uid" = concat('fake_', right(random()::text, 12)), "merchant_uid" = concat('fake_', right(random()::text, 12));

--update "iamport_transaction" set "merchant_uid" = concat('fake_', right(random()::text, 12));
update "subscription_usersubscriptioninvoice" set "merchant_uid" = concat('fake_', right(random()::text, 12));

update "commerce_order" set "requested_shipping_phone_number" = '+821031351310';
update "organization_object" set "data" = jsonb_set("data", '{email}', '"dev@brandazine.kr"') where "type" = 'BUSINESS_REGISTRATION_INFORMATION';
update user_account_login set encrypted_password = '$2a$12$cR7ABtNInV5UxTNk2zK2Jeo0i3LfyThJrTslnOuiKv0tGEVJNk9Km';

update user_account set phone = '+821031351310', encrypted_phone_number = 'unencrypted::+821031351310';

update "user_account_curator_property" set "phone" = '+821031351310', "email" = 'dev+' || split_part("email", '@', 1) || '+' || "username" || '@brandazine.kr', "postcode" = '04514', "address_1" = '서울 중구 서소문로 120 (서소문동, ENA Center', "address_2" = '6층';

update curator_deviceinfo set push_token='f0y-JPhwTT-Wic60baldxt:APA91bFpb-H0Ndr-4HyHz4cY_e0IKvPesPuNptyxqsvA_Gk79xNouDjTeS7P1Oo7u6PwPNWNqCYJG4t_1eHdu7fkTFhsVV1613AzjVbqzwXyphUxXmhPJu2G0lRaaXL0C5i2j6GmbHCQ';

update self_campaign set admin_info = jsonb_set(admin_info, '{phone}', '"+821031351310"');

update "user_account_device_info" set "push_token" = 'invalid-token';

ALTER TABLE "public"."asset" DROP CONSTRAINT "asset_no_dev_bucket_check";

