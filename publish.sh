sam build
sam package --s3-bucket cyruswong-sam-repo --output-template-file packaged.yaml
sam publish --template packaged.yaml