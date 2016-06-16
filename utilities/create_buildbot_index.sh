curl -XDELETE 'http://localhost:9200/buildbot/'

curl -XPUT 'http://localhost:9200/buildbot/' -d '{
    "mappings" : {
        "step" : {
            "properties" : {
                "blamelist" : {"type" : "string"}, 
                "builder" : {"type" : "string", "index" : "not_analyzed"},
                "duration" : {"type" : "double"},
                "end" : {"type" : "date" },
                "name" : {"type" : "string", "index" : "not_analyzed"},
                "number" : {"type" : "long"},
                "project" : {"type" : "string", "index" : "not_analyzed"},
                "result" : {"type" : "string", "index" : "not_analyzed"},
                "slave" : {"type" : "string", "index" : "not_analyzed"},
                "start" : {"type" : "date"},
                "tags" : {"type" : "string"},
                "type" : {"type" : "string", "index" : "not_analyzed"}
            }
        },
        "build" : {
            "properties" : {
                "blamelist" : {"type" : "string"},
                "duration" : {"type" : "double"},
                "end" : {"type" : "date"},
                "name" : {"type" : "string", "index" : "not_analyzed"},
                "number" : {"type" : "long"},
                "project" : {"type" : "string", "index" : "not_analyzed"},
                "result" : {"type" : "string", "index" : "not_analyzed"},
                "slave" : {"type" : "string", "index" : "not_analyzed"},
                "start" : {"type" : "date"},
                "tags" : {"type" : "string"},
                "total_duration" : {"type" : "double"},
                "type" : {"type" : "string", "index" : "not_analyzed"},
                "waiting_duration" : {"type" : "double"}
            }
        }
    }
}'
