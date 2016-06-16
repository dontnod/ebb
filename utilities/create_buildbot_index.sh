curl -XDELETE 'http://localhost:9200/buildbot/'

curl -XPUT 'http://localhost:9200/buildbot/' -d '{
    "mappings" : {
        "step" : {
            "properties" : {
                "type" : {"type" : "string", "index" : "not_analyzed"},
                "step_name" : {"type" : "string", "index" : "not_analyzed"},
				"repository" : {"type" : "string", index : "not_analyzed"},
				"buildername" : {"type" : "string", index : "not_analyzed"},
				"got_revision" : {"type" : "string", index : "not_analyzed"},
				"project" : {"type" : "string", index : "not_analyzed"},
				"slavename" : {"type" : "string", index : "not_analyzed"},
				"branch" : {"type" : "string", index : "not_analyzed"},
				"revision" : {"type" : "string", index : "not_analyzed"},
                "buildnumber" : {"type" : "long"},
                "step_number" : {"type" : "long"},
                "blamelist" : {"type" : "string"}, 
                "start" : {"type" : "date"},
                "end" : {"type" : "date" },
                "duration" : {"type" : "double"},
                "result" : {"type" : "string", "index" : "not_analyzed"}
            }
        },
        "build" : {
            "properties" : {
                "type" : {"type" : "string", "index" : "not_analyzed"},
				"repository" : {"type" : "string", index : "not_analyzed"},
				"buildername" : {"type" : "string", index : "not_analyzed"},
				"got_revision" : {"type" : "string", index : "not_analyzed"},
				"project" : {"type" : "string", index : "not_analyzed"},
				"slavename" : {"type" : "string", index : "not_analyzed"},
				"branch" : {"type" : "string", index : "not_analyzed"},
				"revision" : {"type" : "string", index : "not_analyzed"},
                "buildnumber" : {"type" : "long"},
                "blamelist" : {"type" : "string"},
                "waiting_duration" : {"type" : "double"},
                "total_duration" : {"type" : "double"},
                "start" : {"type" : "date"},
                "end" : {"type" : "date" },
                "duration" : {"type" : "double"}
            }
        }
    }
}'
